/**
 * Resolves a natural-language company reference to validated candidate ticker symbols,
 * CIN, and PAN identifiers.
 */
export interface EntityResolution {
    ticker: string;
    name: string;
    cin: string;
    pan: string;
    exchange: string;
    confidence: number;
}
export declare function resolveEntity(query: string): Promise<EntityResolution[]>;
