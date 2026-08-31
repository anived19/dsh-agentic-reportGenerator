import { Context } from 'cordis';
export declare const name = "entity-guard";
declare module 'cordis' {
    interface SessionEventMap {
        'finoscale/entity-resolved': {
            ticker: string;
            cin?: string;
            pan?: string;
            exchange?: string;
        };
    }
}
export declare function apply(ctx: Context): void;
