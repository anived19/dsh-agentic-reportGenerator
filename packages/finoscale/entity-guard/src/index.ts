import { Context } from 'cordis';

export const name = 'entity-guard';

export function apply(ctx: Context) {
    // Strict monotonic guard logic using native ctx.tools.guard
    ctx.tools.guard(async (exec: any) => {
        const entityId = exec?.agent?.session?.entity_id;
        if (!entityId) {
            throw new Error("FATAL EXCEPTION: Execution halted. Missing 'entity_id' in session log. Hallucination guard triggered.");
        }
    });
}
