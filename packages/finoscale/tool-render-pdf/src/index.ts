import { Context } from 'cordis';
import { exec } from 'child_process';
import { promisify } from 'util';
import * as path from 'path';

const execAsync = promisify(exec);

export const name = 'tool-render-pdf';

export function apply(ctx: Context) {
    ctx.command('render_pdf', 'Renders markdown to PDF via python boundary')
        .action(async ({ session }: any, inputPath: string, outputPath: string) => {
            const pythonScript = path.resolve(process.cwd(), 'python-boundary', 'render.py');
            
            console.log(`[Renderer] Handoff to python boundary: ${pythonScript}`);
            
            try {
                const { stdout, stderr } = await execAsync(`python "${pythonScript}" --input "${inputPath}" --output "${outputPath}"`);
                if (stderr) {
                    console.error("[Renderer STDERR]", stderr);
                }
                console.log("[Renderer STDOUT]", stdout);
                return `Successfully rendered PDF to ${outputPath}`;
            } catch (error) {
                console.error("[Renderer ERROR]", error);
                throw new Error(`Failed to render PDF: ${error}`);
            }
        });
}
