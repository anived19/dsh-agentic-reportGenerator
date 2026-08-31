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

const STATIC_MAP: Record<string, EntityResolution> = {
    "reliance industries": { ticker: "RELIANCE.NS", name: "Reliance Industries", cin: "L17110MH1973PLC019786", pan: "AAACR1234F", exchange: "NSE", confidence: 1.0 },
    "tata consultancy services": { ticker: "TCS.NS", name: "Tata Consultancy Services", cin: "L22210MH1995PLC084781", pan: "AAACT1234F", exchange: "NSE", confidence: 1.0 },
    "tcs": { ticker: "TCS.NS", name: "Tata Consultancy Services", cin: "L22210MH1995PLC084781", pan: "AAACT1234F", exchange: "NSE", confidence: 1.0 },
    "infosys": { ticker: "INFY.NS", name: "Infosys", cin: "L85110KA1981PLC013115", pan: "AAACI1234F", exchange: "NSE", confidence: 1.0 },
    "hdfc bank": { ticker: "HDFCBANK.NS", name: "HDFC Bank", cin: "L65920MH1994PLC080618", pan: "AAACH1234F", exchange: "NSE", confidence: 1.0 },
    "icici bank": { ticker: "ICICIBANK.NS", name: "ICICI Bank", cin: "L65190GJ1994PLC021012", pan: "AAACI5678F", exchange: "NSE", confidence: 1.0 }
    // Note: Other tickers can be mocked similarly or extended.
};

function cleanQuery(query: string): string {
    return query.toLowerCase().replace(/give me a stock report of|report of|company|shares/g, '').replace(/[^\w\s]/g, ' ').replace(/\s+/g, ' ').trim();
}

export async function resolveEntity(query: string): Promise<EntityResolution[]> {
    const rawNorm = query.trim().toLowerCase();
    const cleaned = cleanQuery(query);

    for (const [candidateName, resolution] of Object.entries(STATIC_MAP)) {
        if (candidateName === rawNorm || candidateName === cleaned) {
            return [resolution];
        }
    }

    // Since we are mocking yfinance in TS for this refactor (or assuming DSH plugin does it),
    // we return a fallback if not found.
    return [];
}
