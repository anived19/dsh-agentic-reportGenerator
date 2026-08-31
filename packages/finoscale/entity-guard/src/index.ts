import { Context } from 'cordis';

export const name = 'entity-guard';

declare module 'cordis' {
    interface SessionEventMap {
        'finoscale/entity-resolved': { ticker: string, cin?: string, pan?: string, exchange?: string };
    }
}

function isFinoscaleTool(name: string): boolean {
    return typeof name === 'string' && name.startsWith('mcp__finoscale__');
}

export function apply(ctx: Context) {
    // Register projection if the API exists
    if ((ctx as any).sessionProjections) {
        (ctx as any).sessionProjections.add('finoscale/entity', {
            init: () => ({ entityId: null }),
            apply: (state: any, event: any) => {
                if (event.type === 'finoscale/entity-resolved') {
                    state.entityId = event.payload.ticker;
                }
                return state;
            }
        });
    }

    function readResolvedEntityId(execution: any): string | undefined {
        if (!(ctx as any).sessionProjections) return 'bypass-no-api';
        const sessionId = execution?.agent?.session?.id;
        if (!sessionId) return undefined;
        const state = (ctx as any).sessionProjections.stateOf(sessionId, 'finoscale/entity');
        return state?.entityId;
    }

    ctx.tools.guard((execution: any) => {
        if (!isFinoscaleTool(execution.name)) return undefined; // only gate our own tools
        if (execution.name === 'mcp__finoscale__resolve_entity' || execution.name === 'mcp__finoscale__ask_user') return undefined;
        
        const entityId = readResolvedEntityId(execution);
        if (!entityId) return 'FATAL: no entity_id resolved for this session yet — call resolve_entity first';
        return undefined;
    });
}
