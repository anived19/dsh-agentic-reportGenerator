"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.FinoscaleGuard = exports.name = void 0;
exports.apply = apply;
const cordis_1 = require("@deepseek-ai/cordis");
require("@deepseek-ai/dsh-tools");
exports.name = 'entity-guard';
class FinoscaleGuard extends cordis_1.Service {
    state = new Map();
    constructor(ctx) {
        super(ctx, 'finoscaleGuard');
        ctx.tools.guard((execution) => {
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
        ctx.on('tools/post-execute', async (exec, result, next) => {
            if (exec.name === 'mcp__finoscale__resolve_entity') {
                const sessionId = exec?.agent?.session?.id;
                if (!sessionId)
                    return next();
                let resolvedTicker;
                if (result && typeof result === 'object') {
                    if (result.resolved_ticker) {
                        resolvedTicker = result.resolved_ticker;
                    }
                    else if (Array.isArray(result.content)) {
                        const textContent = result.content.find((c) => c.type === 'text');
                        if (textContent?.text) {
                            try {
                                const parsed = JSON.parse(textContent.text);
                                if (parsed.resolved_ticker) {
                                    resolvedTicker = parsed.resolved_ticker;
                                }
                            }
                            catch (e) {
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
exports.FinoscaleGuard = FinoscaleGuard;
function apply(ctx) {
    if (!ctx.tools || typeof ctx.tools.guard !== 'function') {
        throw new Error("FATAL: Required API ctx.tools.guard is missing.");
    }
    ctx.plugin(FinoscaleGuard);
}
