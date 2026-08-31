"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.name = void 0;
exports.apply = apply;
exports.name = 'dynamic-scoring-plugin';
function apply(ctx) {
    ctx.command('normalize_scores', 'Normalize dynamic scores for missing data')
        .action(async ({ session }, data) => {
        const result = { ...data };
        // Non-Banked Entities
        if (data.activeFBLimits === 0 && data.activeNFBLimits === 0) {
            result.bankingScore = null;
            result.bankingScoreText = "BANKING SCORE: N/A - Entity does not maintain active banking/credit facilities.";
        }
        // Unrated Entities
        if (!data.agencyRating) {
            result.creditRatingText = "CREDIT RATING: Unrated / No Public Agency Rating Available.";
        }
        // Zero Adverse Media
        if (!data.adverseMediaFound) {
            result.adverseMediaText = "Clear Pass: No adverse findings across 60+ regulatory and legal databases.";
        }
        // Non-Corporate Entities
        if (data.entityType === 'LLP' || data.entityType === 'Partnership') {
            result.cinText = "CIN: N/A (LLP / Partnership Entity)";
            result.mcaChecks = "Clear (Not Applicable for Non-Corporate Entities)";
        }
        return result;
    });
}
