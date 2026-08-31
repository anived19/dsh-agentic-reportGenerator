import { Context, Service } from '@deepseek-ai/cordis';
import '@deepseek-ai/dsh-tools';

export const name = 'entity-guard';

declare module '@deepseek-ai/cordis' {
    interface Context {
        finoscaleGuard: FinoscaleGuard;
    }
}

export class FinoscaleGuard extends Service {
    private state = new Map<string, { ticker: string }>();

    constructor(ctx: Context) {
        super(ctx, 'finoscaleGuard');

        ctx.tools.guard((execution: any) => {
            const toolName = execution.name;
            if (typeof toolName !== 'string' || !toolName.startsWith('mcp__finoscale__')) {
                return undefined;
            }
            if (toolName === 'mcp__finoscale__resolve_entity' || toolName === 'mcp__finoscale__ask_user') {
                return undefined;
            }

            const sessionId = execution?.agent?.session?.id;
            if (!sessionId) {
                return 'FATAL: no session id found to track entity resolution';
            }

            const sessionState = this.state.get(sessionId);
            if (!sessionState?.ticker) {
                return 'FATAL: no entity resolved for this session yet — call resolve_entity first';
            }

            return undefined;
        });

        ctx.on('tools/post-execute', async (exec: any, result: any, next: any) => {
            if (exec.name === 'mcp__finoscale__resolve_entity') {
                const sessionId = exec?.agent?.session?.id;
                if (!sessionId) return next();
                
                let resolvedTicker: string | undefined;
                
                if (result && typeof result === 'object') {
                    if (result.resolved_ticker) {
                        resolvedTicker = result.resolved_ticker;
                    } else if (Array.isArray(result.content)) {
                        const textContent = result.content.find((c: any) => c.type === 'text');
                        if (textContent?.text) {
                            try {
                                const parsed = JSON.parse(textContent.text);
                                if (parsed.resolved_ticker) {
                                    resolvedTicker = parsed.resolved_ticker;
                                }
                            } catch (e) {
                                // ignore parse error
                            }
                        }
                    }
                }

                if (resolvedTicker) {
                    this.state.set(sessionId, { ticker: resolvedTicker });
                }
            }
            return next();
        });
    }
}

export function apply(ctx: Context) {
    if (!ctx.tools || typeof ctx.tools.guard !== 'function') {
        throw new Error("FATAL: Required API ctx.tools.guard is missing.");
    }
    ctx.plugin(FinoscaleGuard);
}
