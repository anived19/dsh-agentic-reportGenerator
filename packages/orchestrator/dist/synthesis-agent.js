"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.name = void 0;
exports.apply = apply;
exports.name = 'orchestrator-synthesis';
function apply(ctx) {
    ctx.command('synthesize_report', 'Triggers synthesis agent 4 scorers')
        .action(async ({ session }) => {
        console.log("Synthesis Agent: Spawning 4 scorers isolated...");
        const scorers = ["Finance", "Banking", "Business", "Hygiene"];
        const results = {};
        // Scorer Isolation: instantiated with context: 'fresh'
        for (const scorer of scorers) {
            console.log(`Spawning Scorer: ${scorer}`);
            // pseudo implementation for DSH subagents spawn
            // const res = await ctx.subagents.spawn({ category: scorer, context: 'fresh' });
            // results[scorer] = res;
        }
        return "Synthesis complete.";
    });
}
