# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

from copy import deepcopy
from typing import List

from botbuilder.schema import (
    Activity,
    ActivityTypes,
    ExpectedReplies,
    DeliveryModes,
    SignInConstants,
    TokenExchangeInvokeRequest,
)
from botbuilder.core import BotAdapter, TurnContext, ExtendedUserTokenProvider
from botbuilder.core.card_factory import ContentTypes
from botbuilder.core.skills import SkillConversationIdFactoryOptions
from botbuilder.dialogs import (
    Dialog,
    DialogContext,
    DialogEvents,
    DialogReason,
    DialogInstance,
)
from botframework.connector.token_api.models import TokenExchangeRequest

from .begin_skill_dialog_options import BeginSkillDialogOptions
from .skill_dialog_options import SkillDialogOptions


class SkillDialog(Dialog):
    def __init__(self, dialog_options: SkillDialogOptions, dialog_id: str):
        super().__init__(dialog_id)
        if not dialog_options:
            raise TypeError("SkillDialog.__init__(): dialog_options cannot be None.")

        self.dialog_options = dialog_options
        self._deliver_mode_state_key = "deliverymode"
        self._sso_connection_name_key = "SkillDialog.SSOConnectionName"

    async def begin_dialog(self, dialog_context: DialogContext, options: object = None):
        """
        Method called when a new dialog has been pushed onto the stack and is being activated.
        :param dialog_context: The dialog context for the current turn of conversation.
        :param options: (Optional) additional argument(s) to pass to the dialog being started.
        """
        dialog_args = SkillDialog._validate_begin_dialog_args(options)

        await dialog_context.context.send_trace_activity(
            f"{SkillDialog.__name__}.BeginDialogAsync()",
            label=f"Using activity of type: {dialog_args.activity.type}",
        )

        # Create deep clone of the original activity to avoid altering it before forwarding it.
        skill_activity: Activity = deepcopy(dialog_args.activity)

        # Apply conversation reference and common properties from incoming activity before sending.
        TurnContext.apply_conversation_reference(
            skill_activity,
            TurnContext.get_conversation_reference(dialog_context.context.activity),
            is_incoming=True,
        )

        dialog_context.active_dialog.state[
            self._deliver_mode_state_key
        ] = dialog_args.activity.delivery_mode

        dialog_context.active_dialog.state[
            self._sso_connection_name_key
        ] = dialog_args.connection_name

        # Send the activity to the skill.
        eoc_activity = await self._send_to_skill(
            dialog_context.context, skill_activity, dialog_args.connection_name
        )
        if eoc_activity:
            return await dialog_context.end_dialog(eoc_activity.value)

        return self.end_of_turn

    async def continue_dialog(self, dialog_context: DialogContext):
        await dialog_context.context.send_trace_activity(
            f"{SkillDialog.__name__}.continue_dialog()",
            label=f"ActivityType: {dialog_context.context.activity.type}",
        )

        # Handle EndOfConversation from the skill (this will be sent to the this dialog by the SkillHandler if
        # received from the Skill)
        if dialog_context.context.activity.type == ActivityTypes.end_of_conversation:
            await dialog_context.context.send_trace_activity(
                f"{SkillDialog.__name__}.continue_dialog()",
                label=f"Got {ActivityTypes.end_of_conversation}",
            )

            return await dialog_context.end_dialog(
                dialog_context.context.activity.value
            )

        # Create deep clone of the original activity to avoid altering it before forwarding it.
        skill_activity = deepcopy(dialog_context.context.activity)
        skill_activity.delivery_mode = dialog_context.active_dialog.state[
            self._deliver_mode_state_key
        ]
        connection_name = dialog_context.active_dialog.state[
            self._sso_connection_name_key
        ]

        # Just forward to the remote skill
        eoc_activity = await self._send_to_skill(
            dialog_context.context, skill_activity, connection_name
        )
        if eoc_activity:
            return await dialog_context.end_dialog(eoc_activity.value)

        return self.end_of_turn

    async def reprompt_dialog(  # pylint: disable=unused-argument
        self, context: TurnContext, instance: DialogInstance
    ):
        # Create and send an event to the skill so it can resume the dialog.
        reprompt_event = Activity(
            type=ActivityTypes.event, name=DialogEvents.reprompt_dialog
        )

        # Apply conversation reference and common properties from incoming activity before sending.
        TurnContext.apply_conversation_reference(
            reprompt_event,
            TurnContext.get_conversation_reference(context.activity),
            is_incoming=True,
        )

        # connection Name is not applicable for a RePrompt, as we don't expect as OAuthCard in response.
        await self._send_to_skill(context, reprompt_event)

    async def resume_dialog(  # pylint: disable=unused-argument
        self, dialog_context: "DialogContext", reason: DialogReason, result: object
    ):
        await self.reprompt_dialog(dialog_context.context, dialog_context.active_dialog)
        return self.end_of_turn

    async def end_dialog(
        self, context: TurnContext, instance: DialogInstance, reason: DialogReason
    ):
        # Send of of conversation to the skill if the dialog has been cancelled.
        if reason in (DialogReason.CancelCalled, DialogReason.ReplaceCalled):
            await context.send_trace_activity(
                f"{SkillDialog.__name__}.end_dialog()",
                label=f"ActivityType: {context.activity.type}",
            )
            activity = Activity(type=ActivityTypes.end_of_conversation)

            # Apply conversation reference and common properties from incoming activity before sending.
            TurnContext.apply_conversation_reference(
                activity,
                TurnContext.get_conversation_reference(context.activity),
                is_incoming=True,
            )
            activity.channel_data = context.activity.channel_data
            activity.additional_properties = context.activity.additional_properties

            # connection Name is not applicable for an EndDialog, as we don't expect as OAuthCard in response.
            await self._send_to_skill(context, activity)

        await super().end_dialog(context, instance, reason)

    @staticmethod
    def _validate_begin_dialog_args(options: object) -> BeginSkillDialogOptions:
        if not options:
            raise TypeError("options cannot be None.")

        dialog_args = BeginSkillDialogOptions.from_object(options)

        if not dialog_args:
            raise TypeError(
                "SkillDialog: options object not valid as BeginSkillDialogOptions."
            )

        if not dialog_args.activity:
            raise TypeError(
                "SkillDialog: activity object in options as BeginSkillDialogOptions cannot be None."
            )

        return dialog_args

    async def _send_to_skill(
        self, context: TurnContext, activity: Activity, connection_name: str = None
    ) -> Activity:
        # Create a conversationId to interact with the skill and send the activity
        conversation_id_factory_options = SkillConversationIdFactoryOptions(
            from_bot_oauth_scope=context.turn_state.get(BotAdapter.BOT_OAUTH_SCOPE_KEY),
            from_bot_id=self.dialog_options.bot_id,
            activity=activity,
            bot_framework_skill=self.dialog_options.skill,
        )

        skill_conversation_id = await self.dialog_options.conversation_id_factory.create_skill_conversation_id(
            conversation_id_factory_options
        )

        # Always save state before forwarding
        # (the dialog stack won't get updated with the skillDialog and things won't work if you don't)
        skill_info = self.dialog_options.skill
        await self.dialog_options.conversation_state.save_changes(context, True)

        response = await self.dialog_options.skill_client.post_activity(
            self.dialog_options.bot_id,
            skill_info.app_id,
            skill_info.skill_endpoint,
            self.dialog_options.skill_host_endpoint,
            skill_conversation_id,
            activity,
        )

        # Inspect the skill response status
        if not 200 <= response.status <= 299:
            raise Exception(
                f'Error invoking the skill id: "{skill_info.id}" at "{skill_info.skill_endpoint}"'
                f" (status is {response.status}). \r\n {response.body}"
            )

        eoc_activity: Activity = None
        if activity.delivery_mode == DeliveryModes.expect_replies and response.body:
            # Process replies in the response.Body.
            response.body: List[Activity]
            response.body = ExpectedReplies().deserialize(response.body).activities

            for from_skill_activity in response.body:
                if from_skill_activity.type == ActivityTypes.end_of_conversation:
                    # Capture the EndOfConversation activity if it was sent from skill
                    eoc_activity = from_skill_activity
                elif await self._intercept_oauth_cards(
                    context, from_skill_activity, connection_name
                ):
                    # do nothing. Token exchange succeeded, so no oauthcard needs to be shown to the user
                    pass
                else:
                    # Send the response back to the channel.
                    await context.send_activity(from_skill_activity)

        return eoc_activity

    async def _intercept_oauth_cards(
        self, context: TurnContext, activity: Activity, connection_name: str
    ):
        """
        Tells is if we should intercept the OAuthCard message.
        """
        if not connection_name or not isinstance(
            context.adapter, ExtendedUserTokenProvider
        ):
            return False

        oauth_card_attachment = next(
            attachment
            for attachment in activity.attachments
            if attachment.content_type == ContentTypes.oauth_card
        )
        if oauth_card_attachment:
            oauth_card = oauth_card_attachment.content
            if (
                oauth_card
                and oauth_card.token_exchange_resource
                and oauth_card.token_exchange_resource.uri
            ):
                try:
                    result = await context.adapter.exchange_token(
                        turn_context=context,
                        connection_name=connection_name,
                        user_id=context.activity.from_property.id,
                        exchange_request=TokenExchangeRequest(
                            uri=oauth_card.token_exchange_resource.uri
                        ),
                    )

                    if result and result.token:
                        return await self._send_token_exchange_invoke_to_skill(
                            activity,
                            oauth_card.token_exchange_resource.id,
                            oauth_card.connection_name,
                            result.token,
                        )
                except:
                    return False

        return False

    async def _send_token_exchange_invoke_to_skill(
        self,
        incoming_activity: Activity,
        request_id: str,
        connection_name: str,
        token: str,
    ):
        activity = incoming_activity.create_reply()
        activity.type = ActivityTypes.invoke
        activity.name = SignInConstants.token_exchange_operation_name
        activity.value = TokenExchangeInvokeRequest(
            id=request_id, token=token, connection_name=connection_name,
        )

        # route the activity to the skill
        skill_info = self.dialog_options.skill
        response = await self.dialog_options.skill_client.post_activity(
            self.dialog_options.bot_id,
            skill_info.app_id,
            skill_info.skill_endpoint,
            self.dialog_options.skill_host_endpoint,
            incoming_activity.conversation.id,
            activity,
        )

        # Check response status: true if success, false if failure
        return response.status / 100 == 2
