"""Provider adapters around the application's own interfaces.

Each module here speaks to exactly one external service - Telegram, Gmail - and knows nothing about leads, jobs,
approvals, or the ledger. That is the whole point of the split: a channel adapter may change how a message is
framed, addressed, signed, or retried, and must never acquire an opinion about whether a business may be
contacted. Those decisions live in ``tracking/`` and ``orchestration/`` and are shared by the CLI, the loopback
API, n8n, and any bot.

Adding WhatsApp or a social platform later means adding one module here plus a thin poller or handler that calls
:func:`tracking.inbound.InboundIntake.process` (inbound) or an existing approval path (outbound). It should never
mean a second ledger, a second approval record, or a second copy of a business rule.
"""
