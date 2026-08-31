"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.name = void 0;
exports.apply = apply;
const child_process_1 = require("child_process");
const util_1 = require("util");
const path = __importStar(require("path"));
const execAsync = (0, util_1.promisify)(child_process_1.exec);
exports.name = 'tool-render-pdf';
function apply(ctx) {
    ctx.command('render_pdf', 'Renders markdown to PDF via python boundary')
        .action(async ({ session }, inputPath, outputPath) => {
        const pythonScript = path.resolve(process.cwd(), 'python-boundary', 'render.py');
        console.log(`[Renderer] Handoff to python boundary: ${pythonScript}`);
        try {
            const { stdout, stderr } = await execAsync(`python "${pythonScript}" --input "${inputPath}" --output "${outputPath}"`);
            if (stderr) {
                console.error("[Renderer STDERR]", stderr);
            }
            console.log("[Renderer STDOUT]", stdout);
            return `Successfully rendered PDF to ${outputPath}`;
        }
        catch (error) {
            console.error("[Renderer ERROR]", error);
            throw new Error(`Failed to render PDF: ${error}`);
        }
    });
}
