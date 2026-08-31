import * as lead from './lead-agent';
import * as synthesis from './synthesis-agent';
import * as marketData from './market-data-agent';
import * as amlMedia from './aml-media-agent';

export const name = 'orchestrator-plugin';

export function apply(ctx: any) {
  ctx.plugin(lead);
  ctx.plugin(synthesis);
  ctx.plugin(marketData);
  ctx.plugin(amlMedia);
}
