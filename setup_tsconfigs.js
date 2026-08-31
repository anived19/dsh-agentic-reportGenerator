const fs = require('fs');
const path = require('path');

const rootBaseConfig = {
  compilerOptions: {
    target: "ES2022",
    module: "CommonJS",
    moduleResolution: "node",
    strict: true,
    esModuleInterop: true,
    skipLibCheck: true,
    forceConsistentCasingInFileNames: true,
    declaration: true
  }
};

fs.writeFileSync('tsconfig.base.json', JSON.stringify(rootBaseConfig, null, 2));

const packages = [
  'packages/compliance/tool-aml-sweeps',
  'packages/finoscale/entity-guard',
  'packages/finoscale/tool-finoscale-apis',
  'packages/finoscale/tool-render-pdf',
  'packages/orchestrator',
  'packages/scoring/dynamic-scoring-plugin'
];

const rootConfig = {
  files: [],
  references: packages.map(p => ({ path: `./${p}` }))
};

fs.writeFileSync('tsconfig.json', JSON.stringify(rootConfig, null, 2));

for (const pkg of packages) {
  const depth = pkg.split('/').length;
  const back = Array(depth).fill('..').join('/');
  
  const pkgConfig = {
    extends: `${back}/tsconfig.base.json`,
    compilerOptions: {
      composite: true,
      outDir: "dist",
      rootDir: "src"
    },
    include: ["src/**/*", `${back}/global.d.ts`]
  };
  
  fs.writeFileSync(path.join(pkg, 'tsconfig.json'), JSON.stringify(pkgConfig, null, 2));
}

console.log('Created tsconfigs');
