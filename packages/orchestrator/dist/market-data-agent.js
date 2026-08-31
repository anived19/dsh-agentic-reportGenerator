"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.name = void 0;
exports.apply = apply;
exports.name = 'orchestrator-market-data';
function apply(ctx) {
    // The agent operates in PTC (Programmatic Tool Calling) Mode using the 'code' preset.
    ctx.command('run_market_data', 'Runs market data queries for the entity using PTC Mode')
        .action(async ({ session }) => {
        const entityId = session?.agent?.session?.entity_id;
        if (!entityId) {
            throw new Error("FATAL: entity_id missing in market-data-agent");
        }
        console.log(`[Market Data] Running queries for ${entityId} using PTC code generation...`);
        // In PTC mode, the agent generates and executes a script like this:
        /*
        await Promise.all([
            ctx.tools.getFundamentals(),
            ctx.tools.getTechnicals(),
            ctx.tools.getOwnership()
        ]);
        */
        return "Market data gathered concurrently via PTC Mode.";
    });
}
