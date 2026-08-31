"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.name = void 0;
exports.apply = apply;
exports.name = 'entity-guard';
function isFinoscaleTool(name) {
    return typeof name === 'string' && name.startsWith('mcp__finoscale__');
}
function apply(ctx) {
    // Register projection if the API exists
    if (ctx.sessionProjections) {
        ctx.sessionProjections.add('finoscale/entity', {
            init: () => ({ entityId: null }),
            apply: (state, event) => {
                if (event.type === 'finoscale/entity-resolved') {
                    state.entityId = event.payload.ticker;
                }
                return state;
            }
        });
    }
    function readResolvedEntityId(execution) {
        if (!ctx.sessionProjections)
            return 'bypass-no-api';
        const sessionId = execution?.agent?.session?.id;
        if (!sessionId)
            return undefined;
        const state = ctx.sessionProjections.stateOf(sessionId, 'finoscale/entity');
        return state?.entityId;
    }
    ctx.tools.guard((execution) => {
        if (!isFinoscaleTool(execution.name))
            return undefined; // only gate our own tools
        if (execution.name === 'mcp__finoscale__resolve_entity' || execution.name === 'mcp__finoscale__ask_user')
            return undefined;
        const entityId = readResolvedEntityId(execution);
        if (!entityId)
            return 'FATAL: no entity_id resolved for this session yet — call resolve_entity first';
        return undefined;
    });
}
