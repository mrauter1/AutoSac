from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
import re
from urllib.parse import urlencode

DEFAULT_UI_LOCALE = "en"
SUPPORTED_UI_LOCALES = ("en", "pt-BR")
UI_LOCALE_COOKIE_NAME = "triage_ui_locale"
UI_LOCALE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60

_DEFAULT_DATETIME_FORMAT = "%Y-%m-%d %H:%M UTC"
_PT_BR_DATETIME_FORMAT = "%d/%m/%Y %H:%M UTC"
_HUMANIZE_SEPARATOR_RE = re.compile(r"[_-]+")

_TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "brand.name": "Stage 1 AI Triage",
        "base.nav.queue": "Queue",
        "base.nav.board": "Board",
        "base.nav.my_tickets": "My tickets",
        "base.nav.new_ticket": "New ticket",
        "base.nav.users": "Users",
        "base.nav.slack": "Slack",
        "base.button.logout": "Log out",
        "base.locale_switcher": "Interface language",
        "common.urgent": "Urgent",
        "common.updated": "Updated",
        "common.unknown": "Unknown",
        "common.unassigned": "Unassigned",
        "common.none_yet": "None yet",
        "common.actions": "Actions",
        "common.yes": "Yes",
        "common.no": "No",
        "common.bytes": "{count} bytes",
        "common.step": "Step",
        "field.email": "Email",
        "field.password": "Password",
        "field.new_password": "New password",
        "field.remember_me": "Remember me",
        "field.attachments": "Attachments",
        "field.short_title": "Short title",
        "field.description": "Description",
        "field.message": "Message",
        "field.reply": "Reply",
        "field.note": "Note",
        "field.display_name": "Display name",
        "field.role": "Role",
        "field.status": "Status",
        "field.bot_token": "Bot token",
        "field.slack_user_id": "Slack user ID",
        "field.message_preview_max_chars": "Message preview max chars",
        "field.http_timeout_seconds": "HTTP timeout seconds",
        "field.delivery_batch_size": "Delivery batch size",
        "field.delivery_max_attempts": "Delivery max attempts",
        "field.delivery_stale_lock_seconds": "Delivery stale lock seconds",
        "field.team_name": "Team name",
        "field.team_id": "Team ID",
        "field.bot_user_id": "Bot user ID",
        "field.validated_at": "Validated at",
        "field.updated_at": "Updated at",
        "field.updated_by": "Updated by",
        "field.checked_at": "Checked at",
        "field.error_code": "Error code",
        "field.next_status": "Next status",
        "field.assigned_to": "Assigned to",
        "field.route_target": "Route target",
        "field.route_ai_to_specialist": "Route AI to specialist",
        "field.mark_urgent": "Mark as urgent",
        "hint.attachments_limit": "Up to 3 files, 5 MiB each.",
        "button.sign_in": "Sign in",
        "button.open_ticket": "Open a ticket",
        "button.create_first_ticket": "Create your first ticket",
        "button.send_reply": "Send reply",
        "button.mark_resolved": "Mark resolved",
        "button.create_ticket": "Create ticket",
        "button.open_board_view": "Open board view",
        "button.open_list_view": "Open list view",
        "button.create_user": "Create user",
        "button.edit": "Edit",
        "button.cancel": "Cancel",
        "button.save_changes": "Save changes",
        "button.save_settings": "Save settings",
        "button.disconnect": "Disconnect",
        "button.activate_user": "Activate user",
        "button.inactivate_user": "Inactivate user",
        "button.apply_filters": "Apply filters",
        "button.reset": "Reset",
        "button.rerun_ai": "Rerun AI",
        "button.approve_and_publish": "Approve and publish",
        "button.reject_draft": "Reject draft",
        "button.save_assignment": "Save assignment",
        "button.update_status": "Update status",
        "button.send_public_reply": "Send public reply",
        "button.add_internal_note": "Add internal note",
        "button.use_normal_routing": "Use normal routing",
        "eyebrow.internal_access": "Internal access",
        "eyebrow.requester_view": "Requester view",
        "eyebrow.dev_ti_view": "Dev/TI view",
        "eyebrow.access_administration": "Access administration",
        "login.heading": "Sign in",
        "login.subtitle": "Use your local Stage 1 account credentials.",
        "requester.list.heading": "My tickets",
        "requester.list.last_updated": "Last updated {value}",
        "requester.list.empty_heading": "No tickets yet",
        "requester.list.empty_body": "Start with a short description and optional screenshots.",
        "requester.new.heading": "Open a ticket",
        "requester.new.subtitle": "Describe the issue in your own words. Attachments are optional.",
        "requester.detail.reply_heading": "Reply",
        "ops.list.heading": "Ticket queue",
        "ops.board.heading": "Ops board",
        "ops.users.heading": "Users",
        "ops.users.subtitle": "Admin can create requester and Dev/TI users. Dev/TI can create requester users.",
        "ops.users.create_heading": "Create user",
        "ops.users.current_heading": "Current users",
        "ops.users.manage_hint": "Manage display name, role, password, and access state for requester and Dev/TI users.",
        "ops.users.password_hint": "Leave blank to keep the current password.",
        "ops.users.read_only": "This account is view-only on this screen.",
        "ops.users.status.active": "Active",
        "ops.users.status.inactive": "Inactive",
        "ops.slack.heading": "Slack integration",
        "ops.slack.subtitle": "Configure DB-backed Slack direct-message delivery without editing environment variables.",
        "ops.slack.form_heading": "Slack DM settings",
        "ops.slack.form_hint": "Leave the bot token blank to keep the current stored token.",
        "ops.slack.enabled_label": "Enable Slack DM delivery",
        "ops.slack.bot_token_hint": "A saved token is encrypted at rest and is never rendered back into this page.",
        "ops.slack.notify_ticket_created": "Notify when a ticket is created",
        "ops.slack.notify_public_message_added": "Notify when a public message is added",
        "ops.slack.notify_status_changed": "Notify when ticket status changes",
        "ops.slack.disconnect_heading": "Disconnect Slack",
        "ops.slack.disconnect_hint": "This clears the stored bot token and disables delivery. Workspace metadata is preserved.",
        "ops.slack.status_heading": "Current status",
        "ops.slack.stored_token_label": "Stored bot token",
        "ops.slack.runtime_valid_label": "Runtime config valid",
        "ops.slack.config_error_label": "Runtime config error",
        "ops.slack.config_summary_label": "Runtime config summary",
        "ops.slack.workspace_heading": "Workspace metadata",
        "ops.slack.delivery_health_heading": "Last known delivery health",
        "ops.slack.delivery_summary_label": "Delivery summary",
        "ops.slack.guidance_heading": "Required Slack capabilities",
        "ops.slack.guidance_body": "Use a bot token that supports direct-message open/send behavior. AutoSac validates the token and uses these Slack Web API methods for DM delivery:",
        "ops.rows.requester": "Requester: {value}",
        "ops.rows.assigned": "Assigned: {value}",
        "ops.rows.last_updated": "Last updated {value}",
        "ops.rows.needs_approval": "Needs approval",
        "ops.rows.empty_heading": "No matching tickets",
        "ops.rows.empty_body": "Adjust the filters to expand the queue.",
        "ops.board.unknown_requester": "Unknown requester",
        "ops.board.pending_draft_approval": "Pending draft approval",
        "ops.board.no_tickets": "No tickets",
        "ops.detail.short_title": "Ops",
        "ops.detail.draft_pending_approval": "Draft pending approval",
        "ops.detail.requester": "Requester: {value}",
        "ops.detail.assigned": "Assigned: {value}",
        "ops.detail.updated": "Updated {value}",
        "ops.detail.activity": "Activity",
        "ops.detail.no_activity": "No activity yet.",
        "ops.detail.ai_analysis": "AI analysis",
        "ops.detail.target_kind": "Target kind",
        "ops.detail.latest_run": "Latest run",
        "ops.detail.run_started_at": "Run started at",
        "ops.detail.worker_pid": "Worker PID",
        "ops.detail.last_heartbeat": "Last heartbeat",
        "ops.detail.recovery_attempts": "Recovery attempts",
        "ops.detail.no_runs_yet": "No runs yet",
        "ops.detail.summary": "Summary",
        "ops.detail.internal_summary": "Internal summary",
        "ops.detail.handoff_reason": "Handoff reason",
        "ops.detail.more_analysis": "More analysis",
        "ops.detail.response_confidence": "Response confidence",
        "ops.detail.risk_level": "Risk level",
        "ops.detail.publish_recommendation": "Publish recommendation",
        "ops.detail.legacy_confidence": "Legacy confidence",
        "ops.detail.legacy_impact": "Legacy impact",
        "ops.detail.legacy_development_needed": "Legacy development needed",
        "ops.detail.requester_language": "Requester language",
        "ops.detail.last_action": "Last action",
        "ops.detail.requeue_requested": "Requeue requested",
        "ops.detail.assistant_used": "Assistant used",
        "ops.detail.assistant_specialist": "Assistant specialist",
        "ops.detail.run_error": "Run error",
        "ops.detail.run_warning": "Run warning",
        "ops.detail.risk_rationale": "Risk rationale",
        "ops.detail.relevant_paths": "Relevant repo/docs paths",
        "ops.detail.latest_ai_note": "Latest AI note",
        "ops.detail.analysis_steps": "Analysis steps",
        "ops.detail.latest_run_steps": "Latest run steps",
        "ops.detail.pending_ai_draft": "Pending AI draft",
        "ops.detail.assignment": "Assignment",
        "ops.detail.public_reply": "Public reply",
        "ops.detail.internal_note": "Internal note",
        "filters.status": "Status",
        "filters.all_statuses": "All statuses",
        "filters.route_target": "Route target",
        "filters.all_route_targets": "All route targets",
        "filters.assigned_to": "Assigned to",
        "filters.anyone": "Anyone",
        "filters.urgent_only": "Urgent only",
        "filters.unassigned_only": "Unassigned only",
        "filters.created_by_me": "Created by me",
        "filters.needs_approval": "Needs approval",
        "filters.updated_since_viewed": "Updated since my last view",
        "table.email": "Email",
        "table.name": "Name",
        "table.role": "Role",
        "table.status": "Status",
        "timeline.lane.public": "Public",
        "timeline.lane.internal": "Internal",
        "timeline.lane.status": "Status",
        "timeline.requester_status_changed_to": "Status changed to {status}",
        "timeline.ops_status_changed": "{from_status} -> {to_status}",
        "enum.requester_status.new": "Reviewing",
        "enum.requester_status.ai_triage": "Reviewing",
        "enum.requester_status.waiting_on_user": "Waiting for your reply",
        "enum.requester_status.waiting_on_dev_ti": "Waiting on team",
        "enum.requester_status.resolved": "Resolved",
        "enum.ops_status.new": "New",
        "enum.ops_status.ai_triage": "AI Triage",
        "enum.ops_status.waiting_on_user": "Waiting on User",
        "enum.ops_status.waiting_on_dev_ti": "Waiting on Dev/TI",
        "enum.ops_status.resolved": "Resolved",
        "enum.requester_author.requester": "You",
        "enum.requester_author.dev_ti": "Team",
        "enum.requester_author.ai": "AI",
        "enum.requester_author.system": "System",
        "enum.requester_suffix.requester": "requester",
        "enum.requester_suffix.dev_ti": "DEV/TI",
        "enum.ops_author.requester": "Requester",
        "enum.ops_author.dev_ti": "Dev/TI",
        "enum.ops_author.ai": "AI",
        "enum.ops_author.system": "System",
        "enum.ops_suffix.requester": "Requester",
        "enum.ops_suffix.dev_ti": "Dev/TI",
        "enum.user_role.requester": "Requester",
        "enum.user_role.dev_ti": "Dev/TI",
        "enum.user_role.admin": "Admin",
        "enum.route_target_kind.direct_ai": "Direct AI",
        "enum.route_target_kind.human_assist": "Human assist",
        "enum.ai_run_status.pending": "Pending",
        "enum.ai_run_status.running": "Running",
        "enum.ai_run_status.succeeded": "Succeeded",
        "enum.ai_run_status.human_review": "Human review",
        "enum.ai_run_status.failed": "Failed",
        "enum.ai_run_status.skipped": "Skipped",
        "enum.ai_run_status.superseded": "Superseded",
        "enum.ai_run_step_kind.router": "Router",
        "enum.ai_run_step_kind.selector": "Selector",
        "enum.ai_run_step_kind.specialist": "Specialist",
        "enum.publish_mode.auto_publish": "Auto publish",
        "enum.publish_mode.draft_for_human": "Draft for human",
        "enum.publish_mode.manual_only": "Manual only",
        "enum.response_confidence.very_low": "Very low",
        "enum.response_confidence.low": "Low",
        "enum.response_confidence.medium": "Medium",
        "enum.response_confidence.high": "High",
        "enum.response_confidence.very_high": "Very high",
        "enum.risk_level.none": "None",
        "enum.risk_level.low": "Low",
        "enum.risk_level.medium": "Medium",
        "enum.risk_level.high": "High",
        "enum.risk_level.critical": "Critical",
        "enum.impact_level.low": "Low",
        "enum.impact_level.medium": "Medium",
        "enum.impact_level.high": "High",
        "enum.impact_level.unknown": "Unknown",
        "error.login_expired": "Your login form expired. Please try again.",
        "error.invalid_login_token": "Invalid login form token. Please try again.",
        "error.invalid_credentials": "Invalid email or password.",
        "error.description_required": "Description is required.",
        "error.reply_required": "Reply text is required.",
        "error.attach_max_files": "Attach at most {count} files.",
        "error.file_too_large": "File exceeds the upload size limit.",
        "error.too_many_fields": "Too many form fields.",
        "error.invalid_upload_request": "Invalid upload request.",
        "error.ticket_not_found": "Ticket not found",
        "error.draft_not_found": "Draft not found",
        "error.attachment_not_found": "Attachment not found",
        "error.attachment_ticket_not_found": "Attachment ticket not found",
        "error.attachment_access_denied": "Attachment access denied",
        "error.user_not_found": "User not found",
        "error.auth_required": "Authentication required",
        "error.session_invalid": "Session is no longer valid",
        "error.ticket_access_required": "Ticket access required",
        "error.ops_access_required": "Ops access required",
        "error.admin_access_required": "Admin access required",
        "error.invalid_csrf": "Invalid CSRF token",
        "error.role_creation_forbidden": "You cannot create that role",
        "error.role_assignment_forbidden": "You cannot assign that role",
        "error.user_management_forbidden": "You cannot manage that user",
        "error.invalid_assignee": "Invalid assignee",
        "error.email_required": "Email is required.",
        "error.invalid_email": "Invalid email address.",
        "error.display_name_required": "Display name is required.",
        "error.password_required": "Password is required.",
        "error.password_too_short": "Password must be at least {count} characters.",
        "error.slack_user_id_whitespace_only": "Slack user ID cannot be whitespace only.",
        "error.slack_user_id_exists": "Slack user ID already exists: {slack_user_id}",
        "error.slack_message_preview_max_chars_min": "message_preview_max_chars must be greater than or equal to {count}",
        "error.slack_http_timeout_seconds_range": "http_timeout_seconds must be between {min} and {max} inclusive",
        "error.slack_delivery_batch_size_min": "delivery_batch_size must be greater than or equal to {count}",
        "error.slack_delivery_max_attempts_min": "delivery_max_attempts must be greater than or equal to {count}",
        "error.slack_delivery_stale_lock_seconds_gt_timeout": "delivery_stale_lock_seconds must be greater than http_timeout_seconds",
        "error.slack_token_required": "Slack DM delivery cannot be enabled without a stored bot token",
        "error.slack_auth_test_failed": "Slack auth.test failed.",
        "error.slack_auth_test_failed_code": "Slack auth.test failed: {error_code}",
        "error.slack_auth_test_request_failed": "Slack auth.test request failed.",
        "error.invalid_active_state": "Invalid active state.",
        "error.internal_note_required": "Internal note text is required",
        "error.invalid_ops_reply_next_status": "Invalid reply status: {next_status}",
        "error.invalid_ticket_status": "Invalid ticket status: {next_status}",
        "error.pending_draft_publish_only": "Only pending drafts can be published.",
        "error.invalid_draft_publish_next_status": "Invalid publish status: {next_status}",
        "error.pending_draft_reject_only": "Only pending drafts can be rejected.",
        "error.unsupported_role": "Unsupported role: {role}",
        "error.user_exists": "User already exists: {email}",
        "error.existing_user_not_admin": "Existing user {email} has role {role}, not admin",
        "error.existing_admin_inactive": "Existing admin {email} is inactive",
        "error.existing_admin_display_name_mismatch": "Existing admin {email} has a different display name",
        "error.existing_admin_password_mismatch": "Existing admin {email} has a different password",
        "error.unknown_user": "Unknown user: {email}",
        "error.unknown_route_target": "Unknown route target: {route_target_id}",
        "error.disabled_route_target": "Route target {route_target_id} is disabled for new runs",
        "error.route_target_no_forced_direct_ai": "Route target {route_target_id} does not support forced direct-AI specialist reruns",
        "error.route_target_no_fixed_rerun": "Route target {route_target_id} does not support fixed specialist reruns",
    },
    "pt-BR": {
        "brand.name": "Stage 1 AI Triage",
        "base.nav.queue": "Fila",
        "base.nav.board": "Quadro",
        "base.nav.my_tickets": "Meus tickets",
        "base.nav.new_ticket": "Novo ticket",
        "base.nav.users": "Usuários",
        "base.nav.slack": "Slack",
        "base.button.logout": "Sair",
        "base.locale_switcher": "Idioma da interface",
        "common.urgent": "Urgente",
        "common.updated": "Atualizado",
        "common.unknown": "Desconhecido",
        "common.unassigned": "Não atribuído",
        "common.none_yet": "Ainda não há",
        "common.actions": "Ações",
        "common.yes": "Sim",
        "common.no": "Não",
        "common.bytes": "{count} bytes",
        "common.step": "Etapa",
        "field.email": "E-mail",
        "field.password": "Senha",
        "field.new_password": "Nova senha",
        "field.remember_me": "Lembrar de mim",
        "field.attachments": "Anexos",
        "field.short_title": "Título curto",
        "field.description": "Descrição",
        "field.message": "Mensagem",
        "field.reply": "Resposta",
        "field.note": "Nota",
        "field.display_name": "Nome de exibição",
        "field.role": "Papel",
        "field.status": "Status",
        "field.bot_token": "Token do bot",
        "field.slack_user_id": "ID do usuário no Slack",
        "field.message_preview_max_chars": "Máximo de caracteres da prévia",
        "field.http_timeout_seconds": "Timeout HTTP em segundos",
        "field.delivery_batch_size": "Tamanho do lote de entrega",
        "field.delivery_max_attempts": "Máximo de tentativas de entrega",
        "field.delivery_stale_lock_seconds": "Segundos para lock obsoleto",
        "field.team_name": "Nome do workspace",
        "field.team_id": "ID do workspace",
        "field.bot_user_id": "ID do usuário do bot",
        "field.validated_at": "Validado em",
        "field.updated_at": "Atualizado em",
        "field.updated_by": "Atualizado por",
        "field.checked_at": "Verificado em",
        "field.error_code": "Código de erro",
        "field.next_status": "Próximo status",
        "field.assigned_to": "Atribuído a",
        "field.route_target": "Destino de roteamento",
        "field.route_ai_to_specialist": "Encaminhar IA para especialista",
        "field.mark_urgent": "Marcar como urgente",
        "hint.attachments_limit": "Até 3 arquivos, 5 MiB cada.",
        "button.sign_in": "Entrar",
        "button.open_ticket": "Abrir ticket",
        "button.create_first_ticket": "Criar seu primeiro ticket",
        "button.send_reply": "Enviar resposta",
        "button.mark_resolved": "Marcar como resolvido",
        "button.create_ticket": "Criar ticket",
        "button.open_board_view": "Abrir quadro",
        "button.open_list_view": "Abrir lista",
        "button.create_user": "Criar usuário",
        "button.edit": "Editar",
        "button.cancel": "Cancelar",
        "button.save_changes": "Salvar alterações",
        "button.save_settings": "Salvar configurações",
        "button.disconnect": "Desconectar",
        "button.activate_user": "Ativar usuário",
        "button.inactivate_user": "Inativar usuário",
        "button.apply_filters": "Aplicar filtros",
        "button.reset": "Limpar",
        "button.rerun_ai": "Executar IA novamente",
        "button.approve_and_publish": "Aprovar e publicar",
        "button.reject_draft": "Rejeitar rascunho",
        "button.save_assignment": "Salvar atribuição",
        "button.update_status": "Atualizar status",
        "button.send_public_reply": "Enviar resposta pública",
        "button.add_internal_note": "Adicionar nota interna",
        "button.use_normal_routing": "Usar roteamento normal",
        "eyebrow.internal_access": "Acesso interno",
        "eyebrow.requester_view": "Visão do solicitante",
        "eyebrow.dev_ti_view": "Visão de Dev/TI",
        "eyebrow.access_administration": "Administração de acesso",
        "login.heading": "Entrar",
        "login.subtitle": "Use as credenciais da sua conta local do Stage 1.",
        "requester.list.heading": "Meus tickets",
        "requester.list.last_updated": "Última atualização {value}",
        "requester.list.empty_heading": "Nenhum ticket ainda",
        "requester.list.empty_body": "Comece com uma descrição curta e capturas de tela opcionais.",
        "requester.new.heading": "Abrir ticket",
        "requester.new.subtitle": "Descreva o problema com suas próprias palavras. Os anexos são opcionais.",
        "requester.detail.reply_heading": "Responder",
        "ops.list.heading": "Fila de tickets",
        "ops.board.heading": "Quadro operacional",
        "ops.users.heading": "Usuários",
        "ops.users.subtitle": "Admin pode criar usuários solicitantes e Dev/TI. Dev/TI pode criar usuários solicitantes.",
        "ops.users.create_heading": "Criar usuário",
        "ops.users.current_heading": "Usuários atuais",
        "ops.users.manage_hint": "Gerencie nome de exibição, papel, senha e estado de acesso para usuários solicitantes e Dev/TI.",
        "ops.users.password_hint": "Deixe em branco para manter a senha atual.",
        "ops.users.read_only": "Esta conta é somente leitura nesta tela.",
        "ops.users.status.active": "Ativo",
        "ops.users.status.inactive": "Inativo",
        "ops.slack.heading": "Integração com Slack",
        "ops.slack.subtitle": "Configure a entrega de mensagens diretas do Slack com persistência no banco, sem editar variáveis de ambiente.",
        "ops.slack.form_heading": "Configurações de DM do Slack",
        "ops.slack.form_hint": "Deixe o token do bot em branco para manter o token já armazenado.",
        "ops.slack.enabled_label": "Ativar entrega de DM pelo Slack",
        "ops.slack.bot_token_hint": "O token salvo fica criptografado em repouso e nunca é renderizado novamente nesta página.",
        "ops.slack.notify_ticket_created": "Notificar quando um ticket for criado",
        "ops.slack.notify_public_message_added": "Notificar quando uma mensagem pública for adicionada",
        "ops.slack.notify_status_changed": "Notificar quando o status do ticket mudar",
        "ops.slack.disconnect_heading": "Desconectar Slack",
        "ops.slack.disconnect_hint": "Isso limpa o token salvo do bot e desativa a entrega. Os metadados do workspace são preservados.",
        "ops.slack.status_heading": "Status atual",
        "ops.slack.stored_token_label": "Token do bot armazenado",
        "ops.slack.runtime_valid_label": "Configuração de runtime válida",
        "ops.slack.config_error_label": "Erro de configuração de runtime",
        "ops.slack.config_summary_label": "Resumo da configuração de runtime",
        "ops.slack.workspace_heading": "Metadados do workspace",
        "ops.slack.delivery_health_heading": "Último estado conhecido da entrega",
        "ops.slack.delivery_summary_label": "Resumo da entrega",
        "ops.slack.guidance_heading": "Recursos necessários do Slack",
        "ops.slack.guidance_body": "Use um token de bot que suporte abertura e envio de mensagens diretas. O AutoSac valida o token e usa estes métodos da Slack Web API para entregar DMs:",
        "ops.rows.requester": "Solicitante: {value}",
        "ops.rows.assigned": "Atribuído: {value}",
        "ops.rows.last_updated": "Última atualização {value}",
        "ops.rows.needs_approval": "Precisa de aprovação",
        "ops.rows.empty_heading": "Nenhum ticket correspondente",
        "ops.rows.empty_body": "Ajuste os filtros para ampliar a fila.",
        "ops.board.unknown_requester": "Solicitante desconhecido",
        "ops.board.pending_draft_approval": "Rascunho aguardando aprovação",
        "ops.board.no_tickets": "Sem tickets",
        "ops.detail.short_title": "Operações",
        "ops.detail.draft_pending_approval": "Rascunho aguardando aprovação",
        "ops.detail.requester": "Solicitante: {value}",
        "ops.detail.assigned": "Atribuído: {value}",
        "ops.detail.updated": "Atualizado {value}",
        "ops.detail.activity": "Atividade",
        "ops.detail.no_activity": "Ainda não há atividade.",
        "ops.detail.ai_analysis": "Análise da IA",
        "ops.detail.target_kind": "Tipo de destino",
        "ops.detail.latest_run": "Última execução",
        "ops.detail.run_started_at": "Execução iniciada em",
        "ops.detail.worker_pid": "PID do worker",
        "ops.detail.last_heartbeat": "Último heartbeat",
        "ops.detail.recovery_attempts": "Tentativas de recuperação",
        "ops.detail.no_runs_yet": "Ainda não há execuções",
        "ops.detail.summary": "Resumo",
        "ops.detail.internal_summary": "Resumo interno",
        "ops.detail.handoff_reason": "Motivo do repasse",
        "ops.detail.more_analysis": "Mais análise",
        "ops.detail.response_confidence": "Confiança da resposta",
        "ops.detail.risk_level": "Nível de risco",
        "ops.detail.publish_recommendation": "Recomendação de publicação",
        "ops.detail.legacy_confidence": "Confiança legada",
        "ops.detail.legacy_impact": "Impacto legado",
        "ops.detail.legacy_development_needed": "Desenvolvimento legado necessário",
        "ops.detail.requester_language": "Idioma do solicitante",
        "ops.detail.last_action": "Última ação",
        "ops.detail.requeue_requested": "Reenfileiramento solicitado",
        "ops.detail.assistant_used": "Assistente usado",
        "ops.detail.assistant_specialist": "Especialista assistente",
        "ops.detail.run_error": "Erro da execução",
        "ops.detail.run_warning": "Aviso da execução",
        "ops.detail.risk_rationale": "Justificativa de risco",
        "ops.detail.relevant_paths": "Caminhos relevantes do repositório/documentação",
        "ops.detail.latest_ai_note": "Última nota da IA",
        "ops.detail.analysis_steps": "Etapas da análise",
        "ops.detail.latest_run_steps": "Etapas da última execução",
        "ops.detail.pending_ai_draft": "Rascunho pendente da IA",
        "ops.detail.assignment": "Atribuição",
        "ops.detail.public_reply": "Resposta pública",
        "ops.detail.internal_note": "Nota interna",
        "filters.status": "Status",
        "filters.all_statuses": "Todos os status",
        "filters.route_target": "Destino de roteamento",
        "filters.all_route_targets": "Todos os destinos",
        "filters.assigned_to": "Atribuído a",
        "filters.anyone": "Qualquer pessoa",
        "filters.urgent_only": "Somente urgentes",
        "filters.unassigned_only": "Somente não atribuídos",
        "filters.created_by_me": "Criados por mim",
        "filters.needs_approval": "Precisa de aprovação",
        "filters.updated_since_viewed": "Atualizados desde minha última visualização",
        "table.email": "E-mail",
        "table.name": "Nome",
        "table.role": "Papel",
        "table.status": "Status",
        "timeline.lane.public": "Público",
        "timeline.lane.internal": "Interno",
        "timeline.lane.status": "Status",
        "timeline.requester_status_changed_to": "Status alterado para {status}",
        "timeline.ops_status_changed": "{from_status} -> {to_status}",
        "enum.requester_status.new": "Em análise",
        "enum.requester_status.ai_triage": "Em análise",
        "enum.requester_status.waiting_on_user": "Aguardando sua resposta",
        "enum.requester_status.waiting_on_dev_ti": "Aguardando equipe",
        "enum.requester_status.resolved": "Resolvido",
        "enum.ops_status.new": "Novo",
        "enum.ops_status.ai_triage": "Triagem por IA",
        "enum.ops_status.waiting_on_user": "Aguardando usuário",
        "enum.ops_status.waiting_on_dev_ti": "Aguardando Dev/TI",
        "enum.ops_status.resolved": "Resolvido",
        "enum.requester_author.requester": "Você",
        "enum.requester_author.dev_ti": "Equipe",
        "enum.requester_author.ai": "IA",
        "enum.requester_author.system": "Sistema",
        "enum.requester_suffix.requester": "solicitante",
        "enum.requester_suffix.dev_ti": "Dev/TI",
        "enum.ops_author.requester": "Solicitante",
        "enum.ops_author.dev_ti": "Dev/TI",
        "enum.ops_author.ai": "IA",
        "enum.ops_author.system": "Sistema",
        "enum.ops_suffix.requester": "Solicitante",
        "enum.ops_suffix.dev_ti": "Dev/TI",
        "enum.user_role.requester": "Solicitante",
        "enum.user_role.dev_ti": "Dev/TI",
        "enum.user_role.admin": "Administrador",
        "enum.route_target_kind.direct_ai": "IA direta",
        "enum.route_target_kind.human_assist": "Apoio humano",
        "enum.ai_run_status.pending": "Pendente",
        "enum.ai_run_status.running": "Em execução",
        "enum.ai_run_status.succeeded": "Concluída",
        "enum.ai_run_status.human_review": "Revisão humana",
        "enum.ai_run_status.failed": "Falhou",
        "enum.ai_run_status.skipped": "Ignorada",
        "enum.ai_run_status.superseded": "Substituída",
        "enum.ai_run_step_kind.router": "Roteador",
        "enum.ai_run_step_kind.selector": "Seletor",
        "enum.ai_run_step_kind.specialist": "Especialista",
        "enum.publish_mode.auto_publish": "Publicação automática",
        "enum.publish_mode.draft_for_human": "Rascunho para humano",
        "enum.publish_mode.manual_only": "Somente manual",
        "enum.response_confidence.very_low": "Muito baixa",
        "enum.response_confidence.low": "Baixa",
        "enum.response_confidence.medium": "Média",
        "enum.response_confidence.high": "Alta",
        "enum.response_confidence.very_high": "Muito alta",
        "enum.risk_level.none": "Nenhum",
        "enum.risk_level.low": "Baixo",
        "enum.risk_level.medium": "Médio",
        "enum.risk_level.high": "Alto",
        "enum.risk_level.critical": "Crítico",
        "enum.impact_level.low": "Baixo",
        "enum.impact_level.medium": "Médio",
        "enum.impact_level.high": "Alto",
        "enum.impact_level.unknown": "Desconhecido",
        "error.login_expired": "Seu formulário de login expirou. Tente novamente.",
        "error.invalid_login_token": "Token inválido no formulário de login. Tente novamente.",
        "error.invalid_credentials": "E-mail ou senha inválidos.",
        "error.description_required": "A descrição é obrigatória.",
        "error.reply_required": "O texto da resposta é obrigatório.",
        "error.attach_max_files": "Anexe no máximo {count} arquivos.",
        "error.file_too_large": "O arquivo excede o limite de tamanho para upload.",
        "error.too_many_fields": "Há campos demais no formulário.",
        "error.invalid_upload_request": "Solicitação de upload inválida.",
        "error.ticket_not_found": "Ticket não encontrado",
        "error.draft_not_found": "Rascunho não encontrado",
        "error.attachment_not_found": "Anexo não encontrado",
        "error.attachment_ticket_not_found": "Ticket do anexo não encontrado",
        "error.attachment_access_denied": "Acesso ao anexo negado",
        "error.user_not_found": "Usuário não encontrado",
        "error.auth_required": "Autenticação obrigatória",
        "error.session_invalid": "A sessão não é mais válida",
        "error.ticket_access_required": "Acesso ao ticket é obrigatório",
        "error.ops_access_required": "Acesso de operações é obrigatório",
        "error.admin_access_required": "Acesso de admin é obrigatório",
        "error.invalid_csrf": "Token CSRF inválido",
        "error.role_creation_forbidden": "Você não pode criar esse papel",
        "error.role_assignment_forbidden": "Você não pode atribuir esse papel",
        "error.user_management_forbidden": "Você não pode gerenciar esse usuário",
        "error.invalid_assignee": "Atribuição inválida",
        "error.email_required": "O e-mail é obrigatório.",
        "error.invalid_email": "Endereço de e-mail inválido.",
        "error.display_name_required": "O nome de exibição é obrigatório.",
        "error.password_required": "A senha é obrigatória.",
        "error.password_too_short": "A senha deve ter pelo menos {count} caracteres.",
        "error.slack_user_id_whitespace_only": "O ID do usuário no Slack não pode conter apenas espaços em branco.",
        "error.slack_user_id_exists": "O ID do usuário no Slack já existe: {slack_user_id}",
        "error.slack_message_preview_max_chars_min": "message_preview_max_chars deve ser maior ou igual a {count}",
        "error.slack_http_timeout_seconds_range": "http_timeout_seconds deve ficar entre {min} e {max}, inclusive",
        "error.slack_delivery_batch_size_min": "delivery_batch_size deve ser maior ou igual a {count}",
        "error.slack_delivery_max_attempts_min": "delivery_max_attempts deve ser maior ou igual a {count}",
        "error.slack_delivery_stale_lock_seconds_gt_timeout": "delivery_stale_lock_seconds deve ser maior que http_timeout_seconds",
        "error.slack_token_required": "A entrega de DM do Slack não pode ser ativada sem um token do bot armazenado",
        "error.slack_auth_test_failed": "O Slack auth.test falhou.",
        "error.slack_auth_test_failed_code": "O Slack auth.test falhou: {error_code}",
        "error.slack_auth_test_request_failed": "A requisição Slack auth.test falhou.",
        "error.invalid_active_state": "Estado de ativação inválido.",
        "error.internal_note_required": "O texto da nota interna é obrigatório",
        "error.invalid_ops_reply_next_status": "Status de resposta inválido: {next_status}",
        "error.invalid_ticket_status": "Status do ticket inválido: {next_status}",
        "error.pending_draft_publish_only": "Somente rascunhos pendentes podem ser publicados.",
        "error.invalid_draft_publish_next_status": "Status de publicação inválido: {next_status}",
        "error.pending_draft_reject_only": "Somente rascunhos pendentes podem ser rejeitados.",
        "error.unsupported_role": "Papel não suportado: {role}",
        "error.user_exists": "Usuário já existe: {email}",
        "error.existing_user_not_admin": "O usuário existente {email} tem papel {role}, não admin",
        "error.existing_admin_inactive": "O admin existente {email} está inativo",
        "error.existing_admin_display_name_mismatch": "O admin existente {email} tem um nome de exibição diferente",
        "error.existing_admin_password_mismatch": "O admin existente {email} tem uma senha diferente",
        "error.unknown_user": "Usuário desconhecido: {email}",
        "error.unknown_route_target": "Destino de roteamento desconhecido: {route_target_id}",
        "error.disabled_route_target": "O destino de roteamento {route_target_id} está desativado para novas execuções",
        "error.route_target_no_forced_direct_ai": "O destino de roteamento {route_target_id} não suporta reexecuções forçadas de especialista com IA direta",
        "error.route_target_no_fixed_rerun": "O destino de roteamento {route_target_id} não suporta reexecuções de especialista fixo",
    },
}

_REQUESTER_STATUS_KEYS = {
    "new": "enum.requester_status.new",
    "ai_triage": "enum.requester_status.ai_triage",
    "waiting_on_user": "enum.requester_status.waiting_on_user",
    "waiting_on_dev_ti": "enum.requester_status.waiting_on_dev_ti",
    "resolved": "enum.requester_status.resolved",
}
_OPS_STATUS_KEYS = {
    "new": "enum.ops_status.new",
    "ai_triage": "enum.ops_status.ai_triage",
    "waiting_on_user": "enum.ops_status.waiting_on_user",
    "waiting_on_dev_ti": "enum.ops_status.waiting_on_dev_ti",
    "resolved": "enum.ops_status.resolved",
}
_REQUESTER_AUTHOR_KEYS = {
    "requester": "enum.requester_author.requester",
    "dev_ti": "enum.requester_author.dev_ti",
    "ai": "enum.requester_author.ai",
    "system": "enum.requester_author.system",
}
_REQUESTER_SUFFIX_KEYS = {
    "requester": "enum.requester_suffix.requester",
    "dev_ti": "enum.requester_suffix.dev_ti",
}
_OPS_AUTHOR_KEYS = {
    "requester": "enum.ops_author.requester",
    "dev_ti": "enum.ops_author.dev_ti",
    "ai": "enum.ops_author.ai",
    "system": "enum.ops_author.system",
}
_OPS_SUFFIX_KEYS = {
    "requester": "enum.ops_suffix.requester",
    "dev_ti": "enum.ops_suffix.dev_ti",
}
_USER_ROLE_KEYS = {
    "requester": "enum.user_role.requester",
    "dev_ti": "enum.user_role.dev_ti",
    "admin": "enum.user_role.admin",
}
_ROUTE_TARGET_KIND_KEYS = {
    "direct_ai": "enum.route_target_kind.direct_ai",
    "human_assist": "enum.route_target_kind.human_assist",
}
_AI_RUN_STATUS_KEYS = {
    "pending": "enum.ai_run_status.pending",
    "running": "enum.ai_run_status.running",
    "succeeded": "enum.ai_run_status.succeeded",
    "human_review": "enum.ai_run_status.human_review",
    "failed": "enum.ai_run_status.failed",
    "skipped": "enum.ai_run_status.skipped",
    "superseded": "enum.ai_run_status.superseded",
}
_AI_RUN_STEP_KIND_KEYS = {
    "router": "enum.ai_run_step_kind.router",
    "selector": "enum.ai_run_step_kind.selector",
    "specialist": "enum.ai_run_step_kind.specialist",
}
_PUBLISH_MODE_KEYS = {
    "auto_publish": "enum.publish_mode.auto_publish",
    "draft_for_human": "enum.publish_mode.draft_for_human",
    "manual_only": "enum.publish_mode.manual_only",
}
_RESPONSE_CONFIDENCE_KEYS = {
    "very_low": "enum.response_confidence.very_low",
    "low": "enum.response_confidence.low",
    "medium": "enum.response_confidence.medium",
    "high": "enum.response_confidence.high",
    "very_high": "enum.response_confidence.very_high",
}
_RISK_LEVEL_KEYS = {
    "none": "enum.risk_level.none",
    "low": "enum.risk_level.low",
    "medium": "enum.risk_level.medium",
    "high": "enum.risk_level.high",
    "critical": "enum.risk_level.critical",
}
_IMPACT_LEVEL_KEYS = {
    "low": "enum.impact_level.low",
    "medium": "enum.impact_level.medium",
    "high": "enum.impact_level.high",
    "unknown": "enum.impact_level.unknown",
}

_ERROR_TRANSLATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^Your login form expired\. Please try again\.$"), "error.login_expired"),
    (re.compile(r"^Invalid login form token\. Please try again\.$"), "error.invalid_login_token"),
    (re.compile(r"^Invalid email or password\.$"), "error.invalid_credentials"),
    (re.compile(r"^Description is required\.$"), "error.description_required"),
    (re.compile(r"^Reply text is required\.?$"), "error.reply_required"),
    (re.compile(r"^Attach at most (?P<count>\d+) files\.$"), "error.attach_max_files"),
    (re.compile(r"^Too many files\. Maximum number of files is (?P<count>\d+)\.?$"), "error.attach_max_files"),
    (re.compile(r"^File exceeds MAX_IMAGE_BYTES$"), "error.file_too_large"),
    (re.compile(r"^Part exceeded maximum size of \d+KB\.$"), "error.file_too_large"),
    (re.compile(r"^Too many fields\. Maximum number of fields is \d+\.?$"), "error.too_many_fields"),
    (re.compile(r"^Missing boundary in multipart\.$"), "error.invalid_upload_request"),
    (
        re.compile(r'^The Content-Disposition header field "name" must be provided\.$'),
        "error.invalid_upload_request",
    ),
    (re.compile(r"^Ticket not found$"), "error.ticket_not_found"),
    (re.compile(r"^Draft not found$"), "error.draft_not_found"),
    (re.compile(r"^Attachment not found$"), "error.attachment_not_found"),
    (re.compile(r"^Attachment ticket not found$"), "error.attachment_ticket_not_found"),
    (re.compile(r"^Attachment access denied$"), "error.attachment_access_denied"),
    (re.compile(r"^User not found$"), "error.user_not_found"),
    (re.compile(r"^Authentication required$"), "error.auth_required"),
    (re.compile(r"^Session is no longer valid$"), "error.session_invalid"),
    (re.compile(r"^Ticket access required$"), "error.ticket_access_required"),
    (re.compile(r"^Ops access required$"), "error.ops_access_required"),
    (re.compile(r"^Admin access required$"), "error.admin_access_required"),
    (re.compile(r"^Invalid CSRF token$"), "error.invalid_csrf"),
    (re.compile(r"^You cannot create that role$"), "error.role_creation_forbidden"),
    (re.compile(r"^You cannot assign that role$"), "error.role_assignment_forbidden"),
    (re.compile(r"^You cannot manage that user$"), "error.user_management_forbidden"),
    (re.compile(r"^Invalid assignee$"), "error.invalid_assignee"),
    (re.compile(r"^Email is required\.$"), "error.email_required"),
    (re.compile(r"^Invalid email address\.$"), "error.invalid_email"),
    (re.compile(r"^Display name is required\.$"), "error.display_name_required"),
    (re.compile(r"^Password is required\.$"), "error.password_required"),
    (re.compile(r"^Password must be at least (?P<count>\d+) characters\.$"), "error.password_too_short"),
    (re.compile(r"^Slack user ID cannot be whitespace only\.$"), "error.slack_user_id_whitespace_only"),
    (re.compile(r"^Slack user ID already exists: (?P<slack_user_id>.+)$"), "error.slack_user_id_exists"),
    (
        re.compile(r"^message_preview_max_chars must be greater than or equal to (?P<count>\d+)$"),
        "error.slack_message_preview_max_chars_min",
    ),
    (
        re.compile(r"^http_timeout_seconds must be between (?P<min>\d+) and (?P<max>\d+) inclusive$"),
        "error.slack_http_timeout_seconds_range",
    ),
    (
        re.compile(r"^delivery_batch_size must be greater than or equal to (?P<count>\d+)$"),
        "error.slack_delivery_batch_size_min",
    ),
    (
        re.compile(r"^delivery_max_attempts must be greater than or equal to (?P<count>\d+)$"),
        "error.slack_delivery_max_attempts_min",
    ),
    (
        re.compile(r"^delivery_stale_lock_seconds must be greater than http_timeout_seconds$"),
        "error.slack_delivery_stale_lock_seconds_gt_timeout",
    ),
    (re.compile(r"^Slack DM delivery cannot be enabled without a stored bot token$"), "error.slack_token_required"),
    (re.compile(r"^Slack auth\.test failed\.$"), "error.slack_auth_test_failed"),
    (re.compile(r"^Slack auth\.test failed: (?P<error_code>.+)$"), "error.slack_auth_test_failed_code"),
    (re.compile(r"^Slack auth\.test request failed\.$"), "error.slack_auth_test_request_failed"),
    (re.compile(r"^Invalid active state\.$"), "error.invalid_active_state"),
    (re.compile(r"^Internal note text is required$"), "error.internal_note_required"),
    (re.compile(r"^Invalid ops reply next status: (?P<next_status>.+)$"), "error.invalid_ops_reply_next_status"),
    (re.compile(r"^Invalid ticket status: (?P<next_status>.+)$"), "error.invalid_ticket_status"),
    (re.compile(r"^Only pending drafts can be published\.$"), "error.pending_draft_publish_only"),
    (re.compile(r"^Invalid draft publish next status: (?P<next_status>.+)$"), "error.invalid_draft_publish_next_status"),
    (re.compile(r"^Only pending drafts can be rejected\.$"), "error.pending_draft_reject_only"),
    (re.compile(r"^Unsupported role: (?P<role>.+)$"), "error.unsupported_role"),
    (re.compile(r"^User already exists: (?P<email>.+)$"), "error.user_exists"),
    (re.compile(r"^Existing user (?P<email>.+) has role (?P<role>.+), not admin$"), "error.existing_user_not_admin"),
    (re.compile(r"^Existing admin (?P<email>.+) is inactive$"), "error.existing_admin_inactive"),
    (
        re.compile(r"^Existing admin (?P<email>.+) has a different display name$"),
        "error.existing_admin_display_name_mismatch",
    ),
    (
        re.compile(r"^Existing admin (?P<email>.+) has a different password$"),
        "error.existing_admin_password_mismatch",
    ),
    (re.compile(r"^Unknown user: (?P<email>.+)$"), "error.unknown_user"),
    (re.compile(r"^Unknown route target: (?P<route_target_id>.+)$"), "error.unknown_route_target"),
    (
        re.compile(r"^Route target (?P<route_target_id>.+) is disabled for new runs$"),
        "error.disabled_route_target",
    ),
    (
        re.compile(r"^Route target (?P<route_target_id>.+) does not support forced direct-AI specialist reruns$"),
        "error.route_target_no_forced_direct_ai",
    ),
    (
        re.compile(r"^Route target (?P<route_target_id>.+) does not support fixed specialist reruns$"),
        "error.route_target_no_fixed_rerun",
    ),
)


def translation_catalog(locale: str) -> Mapping[str, str]:
    normalized = normalize_ui_locale(locale) or DEFAULT_UI_LOCALE
    return _TRANSLATIONS[normalized]


def normalize_ui_locale(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    lowered = candidate.lower()
    if lowered == "en" or lowered.startswith("en-"):
        return "en"
    if lowered in {"pt", "pt-br", "pt_br"} or lowered.startswith("pt-"):
        return "pt-BR"
    return None


def _parse_accept_language(value: str | None) -> list[str]:
    if not value:
        return []
    weighted: list[tuple[float, int, str]] = []
    for index, item in enumerate(value.split(",")):
        raw = item.strip()
        if not raw:
            continue
        parts = [part.strip() for part in raw.split(";") if part.strip()]
        locale = parts[0]
        quality = 1.0
        for part in parts[1:]:
            if not part.startswith("q="):
                continue
            try:
                quality = float(part[2:])
            except ValueError:
                quality = 0.0
        weighted.append((quality, index, locale))
    weighted.sort(key=lambda item: (-item[0], item[1]))
    return [locale for _, _, locale in weighted]


def locale_from_accept_language(value: str | None) -> str | None:
    for candidate in _parse_accept_language(value):
        normalized = normalize_ui_locale(candidate)
        if normalized in SUPPORTED_UI_LOCALES:
            return normalized
    return None


def configured_default_ui_locale(default_locale: str | None = None) -> str:
    normalized = normalize_ui_locale(default_locale)
    if normalized in SUPPORTED_UI_LOCALES:
        return normalized
    from shared.config import get_default_ui_locale

    configured = normalize_ui_locale(get_default_ui_locale())
    if configured in SUPPORTED_UI_LOCALES:
        return configured
    return DEFAULT_UI_LOCALE


def resolve_ui_locale(request, default_locale: str | None = None) -> str:
    fallback_locale = configured_default_ui_locale(default_locale)
    cookie_locale = normalize_ui_locale(request.cookies.get(UI_LOCALE_COOKIE_NAME))
    if cookie_locale in SUPPORTED_UI_LOCALES:
        return cookie_locale
    header_locale = locale_from_accept_language(request.headers.get("accept-language"))
    if header_locale in SUPPORTED_UI_LOCALES:
        return header_locale
    return fallback_locale


def translate(locale: str, key: str, **params: object) -> str:
    normalized = normalize_ui_locale(locale) or DEFAULT_UI_LOCALE
    message = _TRANSLATIONS.get(normalized, {}).get(key)
    if message is None:
        message = _TRANSLATIONS[DEFAULT_UI_LOCALE].get(key, key)
    return message.format(**params) if params else message


def get_translator(locale: str) -> Callable[[str], str]:
    def translator(key: str, **params: object) -> str:
        return translate(locale, key, **params)

    return translator


def _humanize_identifier(value: str) -> str:
    return _HUMANIZE_SEPARATOR_RE.sub(" ", value).strip().title()


def _translate_from_mapping(
    *,
    locale: str,
    value: str,
    key_map: Mapping[str, str],
    fallback: Callable[[str], str] | None = None,
) -> str:
    key = key_map.get(value)
    if key is not None:
        return translate(locale, key)
    if fallback is not None:
        return fallback(value)
    return value


def requester_status_label(status: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=status, key_map=_REQUESTER_STATUS_KEYS, fallback=_humanize_identifier)


def requester_author_label(author_type: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(
        locale=locale,
        value=author_type,
        key_map=_REQUESTER_AUTHOR_KEYS,
        fallback=_humanize_identifier,
    )


def requester_role_suffix_label(author_type: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(
        locale=locale,
        value=author_type,
        key_map=_REQUESTER_SUFFIX_KEYS,
        fallback=_humanize_identifier,
    )


def ops_status_label(status: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=status, key_map=_OPS_STATUS_KEYS, fallback=_humanize_identifier)


def ops_author_label(author_type: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=author_type, key_map=_OPS_AUTHOR_KEYS, fallback=_humanize_identifier)


def ops_role_suffix_label(author_type: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=author_type, key_map=_OPS_SUFFIX_KEYS, fallback=_humanize_identifier)


def user_role_label(role: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=role, key_map=_USER_ROLE_KEYS, fallback=_humanize_identifier)


def route_target_kind_label(kind: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=kind, key_map=_ROUTE_TARGET_KIND_KEYS, fallback=_humanize_identifier)


def ai_run_status_label(status: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=status, key_map=_AI_RUN_STATUS_KEYS, fallback=_humanize_identifier)


def ai_run_step_kind_label(kind: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=kind, key_map=_AI_RUN_STEP_KIND_KEYS, fallback=_humanize_identifier)


def publish_mode_recommendation_label(value: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=value, key_map=_PUBLISH_MODE_KEYS, fallback=_humanize_identifier)


def response_confidence_label(value: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=value, key_map=_RESPONSE_CONFIDENCE_KEYS, fallback=_humanize_identifier)


def risk_level_label(value: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=value, key_map=_RISK_LEVEL_KEYS, fallback=_humanize_identifier)


def impact_level_label(value: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return _translate_from_mapping(locale=locale, value=value, key_map=_IMPACT_LEVEL_KEYS, fallback=_humanize_identifier)


def timeline_lane_label(lane: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return translate(locale, f"timeline.lane.{lane}")


def requester_status_change_summary(to_status_label: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    return translate(locale, "timeline.requester_status_changed_to", status=to_status_label)


def ops_status_change_summary(
    from_status_label: str | None,
    to_status_label: str,
    locale: str = DEFAULT_UI_LOCALE,
) -> str:
    if not from_status_label:
        return requester_status_change_summary(to_status_label, locale)
    return translate(
        locale,
        "timeline.ops_status_changed",
        from_status=from_status_label,
        to_status=to_status_label,
    )


def bool_label(value: bool, locale: str = DEFAULT_UI_LOCALE) -> str:
    return translate(locale, "common.yes" if value else "common.no")


def unknown_label(locale: str = DEFAULT_UI_LOCALE) -> str:
    return translate(locale, "common.unknown")


def unassigned_label(locale: str = DEFAULT_UI_LOCALE) -> str:
    return translate(locale, "common.unassigned")


def none_yet_label(locale: str = DEFAULT_UI_LOCALE) -> str:
    return translate(locale, "common.none_yet")


def format_datetime_utc(value: Any, locale: str = DEFAULT_UI_LOCALE) -> str:
    format_string = _PT_BR_DATETIME_FORMAT if normalize_ui_locale(locale) == "pt-BR" else _DEFAULT_DATETIME_FORMAT
    return value.strftime(format_string)


def current_request_path(request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def build_locale_switch_links(request, next_path: str | None = None) -> dict[str, str]:
    current_path = sanitize_ui_switch_path(next_path) or current_request_path(request)
    return {
        locale: f"/ui-language?{urlencode({'locale': locale, 'next': current_path})}"
        for locale in SUPPORTED_UI_LOCALES
    }


def sanitize_ui_switch_path(path: str | None) -> str | None:
    if path is None:
        return None
    candidate = path.strip()
    if not candidate:
        return None
    if not candidate.startswith("/"):
        return None
    return candidate


def translate_error_text(message: str, locale: str = DEFAULT_UI_LOCALE) -> str:
    for pattern, key in _ERROR_TRANSLATION_PATTERNS:
        match = pattern.match(message)
        if match is None:
            continue
        return translate(locale, key, **match.groupdict())
    return message
