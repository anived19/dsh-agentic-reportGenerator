"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.name = void 0;
exports.apply = apply;
exports.name = 'entity-guard';
function apply(ctx) {
    // Strict monotonic guard logic using native ctx.tools.guard
    ctx.tools.guard(async (exec) => {
        const entityId = exec?.agent?.session?.entity_id;
        if (!entityId) {
            throw new Error("FATAL EXCEPTION: Execution halted. Missing 'entity_id' in session log. Hallucination guard triggered.");
        }
    });
}
