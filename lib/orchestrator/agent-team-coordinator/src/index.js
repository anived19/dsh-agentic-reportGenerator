"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.name = void 0;
exports.apply = apply;
exports.name = 'finoscale-agent-team-coordinator';
function apply(ctx) {
    // Define the Lead/Coordinator Agent via DSH Agent Teams topological logic
    // Note: Using DSH experimental agent teams `ctx.agentTeams` structure
    // This simulates the topology defined in the architecture specification.
    ctx.on('ready', () => {
        if (ctx.agentTeams) {
            ctx.agentTeams.registerTeam('CreditReportTeam', {
                roles: {
                    'LeadAgent': {
                        description: 'Coordinator that spawns Market Data and AML agents.',
                        execute: async (session) => {
                            // 1. Spawns Market Data Agent
                            const marketTask = ctx.agentTeams.spawnTeammate('MarketDataAgent', { context: 'fresh' });
                            // 2. Spawns AML / Media Agent
                            const amlTask = ctx.agentTeams.spawnTeammate('AMLMediaAgent', { context: 'fresh' });
                            // 3. Wait for data gathering
                            await Promise.all([marketTask, amlTask]);
                            // 4. Spawn Synthesis Agent
                            await ctx.agentTeams.spawnTeammate('SynthesisAgent', { context: 'fresh' });
                        }
                    },
                    'MarketDataAgent': {
                        description: 'Fetches all financial metrics using PTC mode.',
                        tools: ['finoscale_get_price_snapshot', 'finoscale_get_fundamentals']
                    },
                    'AMLMediaAgent': {
                        description: 'Performs regulatory and AML checks using PTC mode.',
                        tools: ['finoscale_run_structured_aml_sweep']
                    },
                    'SynthesisAgent': {
                        description: 'Consolidates state and spawns 4 scorer sub-agents, applies skill layout.',
                        execute: async (session) => {
                            // Spawns 4 dedicated scorers
                            await Promise.all([
                                ctx.agentTeams.spawnTeammate('FinanceScorer', { context: 'fresh' }),
                                ctx.agentTeams.spawnTeammate('BankingScorer', { context: 'fresh' }),
                                ctx.agentTeams.spawnTeammate('BusinessScorer', { context: 'fresh' }),
                                ctx.agentTeams.spawnTeammate('HygieneScorer', { context: 'fresh' })
                            ]);
                            // Trigger formatting and PDF render
                            // Uses skill `credit-report-format.md` implicitly via prompt matching
                            await session.executeTool('finoscale_tool_render_pdf', { report_data: {} });
                        }
                    }
                }
            });
        }
    });
}
