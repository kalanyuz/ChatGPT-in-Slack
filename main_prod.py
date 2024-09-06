#
# Imports
#

import json
import logging
import os
from openai import OpenAI

from slack_sdk.web import WebClient
from slack_sdk.errors import SlackApiError
from slack_bolt.oauth.oauth_settings import OAuthSettings
from slack_sdk.oauth.installation_store import FileInstallationStore
from slack_sdk.oauth.state_store import FileOAuthStateStore
from slack_bolt import App, Ack, BoltContext

from app.bolt_listeners import register_listeners, before_authorize
from app.env import (
    USE_SLACK_LANGUAGE,
    SLACK_APP_LOG_LEVEL,
    OPENAI_MODEL,
    OPENAI_TEMPERATURE,
    OPENAI_API_TYPE,
    OPENAI_API_BASE,
    OPENAI_API_VERSION,
    OPENAI_DEPLOYMENT_ID,
    OPENAI_FUNCTION_CALL_MODULE_NAME,
    OPENAI_ORG_ID,
    OPENAI_IMAGE_GENERATION_MODEL,
)
from app.slack_ui import (
    build_home_tab,
    DEFAULT_HOME_TAB_MESSAGE,
    build_configure_modal,
)
from app.i18n import translate
from flask import Flask, request

from google.cloud import storage
from slack_bolt.adapter.flask import SlackRequestHandler

logging.basicConfig(format="%(asctime)s %(message)s", level=SLACK_APP_LOG_LEVEL)

storage_client = storage.Client(project=os.environ["GOOGLE_CLOUD_PROJECT"])
openai_bucket_name = os.environ["OPENAI_GCS_BUCKET_NAME"]

# Use the mounted path for installation and state stores
MOUNT_PATH = os.environ.get("MOUNT_PATH", "/mnt/data")

def register_revocation_handlers(app: App):
    # Handle uninstall events and token revocations
    @app.event("tokens_revoked")
    def handle_tokens_revoked_events(
        event: dict,
        context: BoltContext,
        logger: logging.Logger,
    ):
        user_ids = event.get("tokens", {}).get("oauth", [])
        if len(user_ids) > 0:
            for user_id in user_ids:
                app.installation_store.delete_installation(
                    enterprise_id=context.enterprise_id,
                    team_id=context.team_id,
                    user_id=user_id,
                )
        bots = event.get("tokens", {}).get("bot", [])
        if len(bots) > 0:
            app.installation_store.delete_bot(
                enterprise_id=context.enterprise_id,
                team_id=context.team_id,
            )
            try:
                bucket = storage_client.bucket(openai_bucket_name)
                blob = bucket.blob(context.team_id)
                blob.delete()
            except Exception as e:
                logger.error(
                    f"Failed to delete an OpenAI auth key: (team_id: {context.team_id}, error: {e})"
                )

    @app.event("app_uninstalled")
    def handle_app_uninstalled_events(
        context: BoltContext,
        logger: logging.Logger,
    ):
        app.installation_store.delete_all(
            enterprise_id=context.enterprise_id,
            team_id=context.team_id,
        )
        try:
            bucket = storage_client.bucket(openai_bucket_name)
            blob = bucket.blob(context.team_id)
            blob.delete()
        except Exception as e:
            logger.error(
                f"Failed to delete an OpenAI auth key: (team_id: {context.team_id}, error: {e})"
            )

    
    @app.middleware
    def set_gcs_openai_api_key(context: BoltContext, next_):
        """
        Warning:
        The configuration with the OpenAI API key will be written to Google Cloud Storage (GCS) as plaintext.
        Therefore, secure access to GCS is required to protect the API key from unauthorized access.
        """
        try:
            bucket = storage_client.bucket(openai_bucket_name)
            blob = bucket.blob(context.team_id)
            config_str = blob.download_as_text()
            if config_str.startswith("{"):
                config = json.loads(config_str)
                context["OPENAI_API_KEY"] = config.get("api_key")
                context["OPENAI_MODEL"] = config.get("model")
                context["OPENAI_IMAGE_GENERATION_MODEL"] = config.get(
                    "image_generation_model", OPENAI_IMAGE_GENERATION_MODEL
                )
                context["OPENAI_TEMPERATURE"] = config.get(
                    "temperature", OPENAI_TEMPERATURE
                )
            else:
                # The legacy data format
                context["OPENAI_API_KEY"] = config_str
                context["OPENAI_MODEL"] = OPENAI_MODEL
                context["OPENAI_IMAGE_GENERATION_MODEL"] = OPENAI_IMAGE_GENERATION_MODEL
                context["OPENAI_TEMPERATURE"] = OPENAI_TEMPERATURE
        except:  # noqa: E722
            context["OPENAI_API_KEY"] = None
            context["OPENAI_MODEL"] = None
            context["OPENAI_IMAGE_GENERATION_MODEL"] = None
            context["OPENAI_TEMPERATURE"] = None

        context["OPENAI_API_TYPE"] = OPENAI_API_TYPE
        context["OPENAI_API_BASE"] = OPENAI_API_BASE
        context["OPENAI_API_VERSION"] = OPENAI_API_VERSION
        context["OPENAI_DEPLOYMENT_ID"] = OPENAI_DEPLOYMENT_ID
        context["OPENAI_ORG_ID"] = OPENAI_ORG_ID
        context["OPENAI_FUNCTION_CALL_MODULE_NAME"] = OPENAI_FUNCTION_CALL_MODULE_NAME
        next_()

    #
    # Home tab rendering
    #

    @app.event("app_home_opened")
    def render_home_tab(client: WebClient, context: BoltContext):
        message = DEFAULT_HOME_TAB_MESSAGE
        try:
            bucket = storage_client.bucket(openai_bucket_name)
            blob = bucket.blob(context.team_id)
            blob.download_as_text()
            message = "This app is ready to use in this workspace :raised_hands:"
        except:  # noqa: E722
            pass
        openai_api_key = context.get("OPENAI_API_KEY")
        client.views_publish(
            user_id=context.user_id,
            view=build_home_tab(
                openai_api_key=openai_api_key,
                context=context,
                message=message,
            ),
        )

    #
    # Configure
    #

    @app.action("configure")
    def handle_configure_button(
        ack, body: dict, client: WebClient, context: BoltContext
    ):
        ack()
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_configure_modal(context),
        )

    def validate_api_key_registration(ack: Ack, view: dict, context: BoltContext):
        already_set_api_key = context.get("OPENAI_API_KEY")

        inputs = view["state"]["values"]
        api_key = inputs["api_key"]["input"]["value"]
        model = inputs["model"]["input"]["selected_option"]["value"]
        try:
            # Verify if the API key is valid
            client = OpenAI(api_key=api_key)
            client.models.retrieve(model="gpt-3.5-turbo")
            try:
                # Verify if the given model works with the API key
                client.models.retrieve(model=model)
            except Exception:
                text = "This model is not yet available for this API key"
                if already_set_api_key is not None:
                    text = translate(
                        openai_api_key=already_set_api_key, context=context, text=text
                    )
                ack(
                    response_action="errors",
                    errors={"model": text},
                )
                return
            ack()
        except Exception:
            text = "This API key seems to be invalid"
            if already_set_api_key is not None:
                text = translate(
                    openai_api_key=already_set_api_key, context=context, text=text
                )
            ack(
                response_action="errors",
                errors={"api_key": text},
            )

    def save_api_key_registration(
        view: dict,
        logger: logging.Logger,
        context: BoltContext,
    ):
        inputs = view["state"]["values"]
        api_key = inputs["api_key"]["input"]["value"]
        model = inputs["model"]["input"]["selected_option"]["value"]
        try:
            client = OpenAI(api_key=api_key)
            client.models.retrieve(model=model)
            bucket = storage_client.bucket(openai_bucket_name)
            blob = bucket.blob(context.team_id)
            blob.upload_from_string(
                json.dumps({"api_key": api_key, "model": model})
            )
        except Exception as e:
            logger.exception(e)

    app.view("configure")(
        ack=validate_api_key_registration,
        lazy=[save_api_key_registration],
    )

app = App(
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
    token=os.environ.get("SLACK_BOT_TOKEN"),
    before_authorize=before_authorize,
    oauth_settings=OAuthSettings(
        client_id=os.environ["SLACK_CLIENT_ID"],
        client_secret=os.environ["SLACK_CLIENT_SECRET"],
        scopes=[
            "commands",
            "app_mentions:read",
            "channels:history",
            "groups:history",
            "im:history",
            "mpim:history",
            "chat:write.public",
            "chat:write",
            "users:read",
            "files:read",
            "files:write",
            "im:write",
        ],
        installation_store=FileInstallationStore(
            base_dir=f"{MOUNT_PATH}/installations"
        ),
        state_store=FileOAuthStateStore(
            expiration_seconds=600, base_dir=f"{MOUNT_PATH}/states"
        ),
    ),
)
register_listeners(app)
register_revocation_handlers(app)

if USE_SLACK_LANGUAGE is True:

    @app.middleware
    def set_locale(
        context: BoltContext,
        client: WebClient,
        logger: logging.Logger,
        next_,
    ):
        bot_scopes = context.authorize_result.bot_scopes
        if bot_scopes is not None and "users:read" in bot_scopes:
            user_id = context.actor_user_id or context.user_id
            try:
                user_info = client.users_info(user=user_id, include_locale=True)
                context["locale"] = user_info.get("user", {}).get("locale")
            except SlackApiError as e:
                logger.debug(f"Failed to fetch user info due to {e}")
                pass
            next_()

flask_app = Flask(__name__)
handler = SlackRequestHandler(app)


@flask_app.route("/health")
def health_check():
    return "OK", 200


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/slack/install", methods=["GET"])
def install():
    return handler.handle(request)


@flask_app.route("/slack/oauth_redirect", methods=["GET"])
def oauth_redirect():
    return handler.handle(request)

if __name__ == "__main__":
    flask_app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
