import { Context } from '@deepseek-ai/dsh';
export declare const name = "finoscale-dynamic-scoring";
export interface ScoringResult {
    financialScore: number | null;
    businessMgmtScore: number | null;
    hygieneScore: number | null;
    bankingScore: number | null;
    bankingScoreReason?: string;
    finalScore: number;
}
export declare function calculateOverallScore(scores: Omit<ScoringResult, 'finalScore'>): ScoringResult;
export declare function apply(ctx: Context): void;
