"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.name = void 0;
exports.apply = apply;
const child_process_1 = require("child_process");
const fs_1 = require("fs");
const path_1 = require("path");
const util_1 = require("util");
const execAsync = (0, util_1.promisify)(child_process_1.exec);
exports.name = 'finoscale-tool-render-pdf';
function apply(ctx) {
    ctx.tools.defineTool('finoscale_tool_render_pdf', {
        description: 'Renders the final formatted Markdown report to a PDF by calling the Weasyprint python subprocess.',
        parameters: {
            type: 'object',
            properties: {
                report_data: {
                    type: 'object',
                    description: 'The JSON data representing the final report including the markdown_body.'
                }
            },
            required: ['report_data']
        }
    }, async (args) => {
        try {
            const inputPath = (0, path_1.join)(process.cwd(), 'temp_report_input.json');
            const outputPath = (0, path_1.join)(process.cwd(), `report_${Date.now()}.pdf`);
            (0, fs_1.writeFileSync)(inputPath, JSON.stringify(args.report_data));
            // Execute the python subprocess
            const { stdout, stderr } = await execAsync(`python python-boundary/render.py --input ${inputPath} --output ${outputPath}`);
            ctx.logger('tool-render-pdf').info(stdout);
            if (stderr) {
                ctx.logger('tool-render-pdf').warn(stderr);
            }
            return { status: 'success', pdf_path: outputPath };
        }
        catch (error) {
            return { status: 'error', error: String(error) };
        }
    });
}
