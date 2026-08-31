import 'cordis';

declare module 'cordis' {
  interface Context {
    command(name: string, desc?: string): any;
    agentTeams: any;
    tools: any;
  }
}
