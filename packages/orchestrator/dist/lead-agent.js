"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.name = void 0;
exports.apply = apply;
exports.name = 'orchestrator-lead';
function apply(ctx) {
    ctx.command('lead_start', 'Starts the orchestrator pipeline')
        .action(async ({ session }, query) => {
        console.log("Lead Agent starting resolution for:", query);
        // 1. Resolve Entity
        const entityId = "TCS.NS"; // Mock deterministic resolution
        // Write entity_id to session log securely
        if (!session.agent)
            session.agent = { session: {} };
        session.agent.session.entity_id = entityId;
        // 2. Spawn Market Data Agent (Teammate - preset 'code')
        // Using standard TypeScript await to enforce the sequential lock.
        console.log("Spawning Market Data Teammate...");
        await ctx.agentTeams.spawnTeammate({
            preset: 'code',
            category: 'MarketData',
            task: `Write a script to batch Finoscale financial lookups using Promise.all() for entity: ${entityId}`
        });
        // 3. Spawn AML/Media Agent
        console.log("Spawning AML/Media Teammate...");
        await ctx.agentTeams.spawnTeammate({
            preset: 'code',
            category: 'AMLMedia',
            task: `Execute regulatory sweeps natively using Promise.all() for entity: ${entityId}`
        });
        // 4. Spawn Synthesis Agent
        console.log("Spawning Synthesis Teammate...");
        await ctx.agentTeams.spawnTeammate({
            category: 'Synthesis',
            task: `Consolidate state and generate the final 13-section report`
        });
        return `Pipeline finished sequentially for ${entityId}`;
    });
}
