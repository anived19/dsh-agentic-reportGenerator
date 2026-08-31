import { Context } from '@deepseek-ai/cordis';
import assert from 'node:assert';
import * as entityGuard from '../src/index';

async function runTest() {
    const ctx = new Context();
    
    // Mock tools API
    const guards: any[] = [];
    
    (ctx as any).tools = {
        guard: (fn: any) => guards.push(fn)
    };
    
    // Instead of full lifecycle, manually instantiate the service to register listeners
    const guardService = new entityGuard.FinoscaleGuard(ctx);
    const guardFn = guards[0];
    assert(guardFn, 'Guard was not registered');

    // Test 1: non-resolve tool before resolution
    const exec1 = {
        name: 'mcp__finoscale__get_price_snapshot',
        agent: { session: { id: 'session-123' } }
    };
    const denied1 = guardFn(exec1);
    assert.strictEqual(typeof denied1, 'string', 'Should deny get_price_snapshot before resolution');
    assert(denied1.includes('FATAL'), 'Should have a clear FATAL message');

    // Test 2: resolve_entity is unconditionally allowed
    const execResolve = {
        name: 'mcp__finoscale__resolve_entity',
        agent: { session: { id: 'session-123' } }
    };
    const denied2 = guardFn(execResolve);
    assert.strictEqual(denied2, undefined, 'resolve_entity should be allowed');

    // Trigger post-execute for resolve_entity by emitting the event
    await ctx.serial('tools/post-execute' as any, execResolve, {
        resolved_ticker: 'AAPL'
    }, () => Promise.resolve());

    // Test 3: non-resolve tool after resolution
    const exec3 = {
        name: 'mcp__finoscale__get_price_snapshot',
        agent: { session: { id: 'session-123' } }
    };
    const denied3 = guardFn(exec3);
    assert.strictEqual(denied3, undefined, 'Should allow get_price_snapshot after resolution');

    // Test 4: Different session ID should still be denied
    const exec4 = {
        name: 'mcp__finoscale__get_price_snapshot',
        agent: { session: { id: 'session-456' } }
    };
    const denied4 = guardFn(exec4);
    assert.strictEqual(typeof denied4, 'string', 'Should deny for a different session');

    console.log('✅ All tests passed!');
}

runTest().catch((err) => {
    console.error('Test failed:', err);
    process.exit(1);
});
