import { Context } from 'cordis';

export const name = 'orchestrator-aml-media';

export function apply(ctx: Context) {
    // The agent operates in PTC (Programmatic Tool Calling) Mode using the 'code' preset.
    ctx.command('run_aml_media', 'Runs AML and media sweeps for the entity using PTC Mode')
        .action(async ({ session }: any) => {
            const entityId = session?.agent?.session?.entity_id;
            
            if (!entityId) {
                throw new Error("FATAL: entity_id missing in aml-media-agent");
            }

            console.log(`[AML/Media] Running sweeps for ${entityId} using PTC code generation...`);
            
            // In PTC mode, the agent generates and executes a script like this:
            /*
            await Promise.all([
                ctx.tools.run_structured_aml_sweep(),
                ctx.tools.search_adverse_media()
            ]);
            */
            
            return "AML/Media sweeps gathered concurrently via PTC Mode.";
        });
}
