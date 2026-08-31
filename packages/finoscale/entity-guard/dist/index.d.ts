import { Context, Service } from '@deepseek-ai/cordis';
import '@deepseek-ai/dsh-tools';
export declare const name = "entity-guard";
declare module '@deepseek-ai/cordis' {
    interface Context {
        finoscaleGuard: FinoscaleGuard;
    }
}
export declare class FinoscaleGuard extends Service {
    private state;
    constructor(ctx: Context);
}
export declare function apply(ctx: Context): void;
