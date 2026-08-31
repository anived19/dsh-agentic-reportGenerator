"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.name = void 0;
exports.calculateOverallScore = calculateOverallScore;
exports.apply = apply;
exports.name = 'finoscale-dynamic-scoring';
function calculateOverallScore(scores) {
    const activeScores = [];
    if (scores.financialScore !== null)
        activeScores.push(scores.financialScore);
    if (scores.businessMgmtScore !== null)
        activeScores.push(scores.businessMgmtScore);
    if (scores.hygieneScore !== null)
        activeScores.push(scores.hygieneScore);
    if (scores.bankingScore !== null)
        activeScores.push(scores.bankingScore);
    const finalScore = activeScores.length > 0
        ? Math.round(activeScores.reduce((a, b) => a + b, 0) / activeScores.length)
        : 0;
    return { ...scores, finalScore };
}
function apply(ctx) {
    // Expose calculating logic as a tool or an internal service 
    ctx.tools.defineTool('finoscale_calculate_score', {
        description: 'Calculates the overall Finoscale Score based on individual modular scores. Handles missing or NA scores automatically.',
        parameters: {
            type: 'object',
            properties: {
                financialScore: { type: ['number', 'null'] },
                businessMgmtScore: { type: ['number', 'null'] },
                hygieneScore: { type: ['number', 'null'] },
                bankingScore: { type: ['number', 'null'] },
                bankingScoreReason: { type: 'string' }
            },
            required: ['financialScore', 'businessMgmtScore', 'hygieneScore', 'bankingScore']
        }
    }, async (args) => {
        return calculateOverallScore(args);
    });
}
