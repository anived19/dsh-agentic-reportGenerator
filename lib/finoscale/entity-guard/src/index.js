"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.name = void 0;
exports.apply = apply;
exports.name = 'finoscale-entity-guard';
function apply(ctx) {
    ctx.tools.guard(async (session, tool, args) => {
        // Only intercept finoscale endpoints (e.g. ones that fetch entity-specific data)
        if (tool.name.startsWith('finoscale_') || tool.name.startsWith('mcp__finoscale__')) {
            // Validate that the entity ID exists in the durable session state
            const entityId = session.state?.entity_id;
            if (!entityId) {
                throw new Error(`[Monotonic Guard] Rejected tool call ${tool.name}. ` +
                    `Entity resolution has not been completed. The session state is missing 'entity_id'. ` +
                    `Do not rely on the model-provided arguments like args.ticker or args.cin.`);
            }
            // Enforce that tools use the session's entityId, disregarding what the model passed
            // In practice, we might override args here or just validate it matches.
            if (args.ticker && args.ticker !== entityId) {
                ctx.logger('entity-guard').warn(`Overriding model-provided ticker ${args.ticker} with session entity ${entityId}`);
                args.ticker = entityId;
            }
            if (args.cin && args.cin !== entityId) {
                ctx.logger('entity-guard').warn(`Overriding model-provided cin ${args.cin} with session entity ${entityId}`);
                args.cin = entityId;
            }
        }
    });
}
