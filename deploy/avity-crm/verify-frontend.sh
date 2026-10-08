#!/bin/sh
set -eu
cd /app
npx esbuild packages/twenty-oxlint-rules/oxlint-plugin.ts --bundle --format=esm \
  --platform=node --outfile=packages/twenty-oxlint-rules/dist/oxlint-plugin.mjs \
  "--define:__filename='\"[plugin]\"'" "--define:__dirname='\"[plugin]\"'" \
  --banner:js="import { createRequire } from 'module'; const require = createRequire(import.meta.url);"
cd packages/twenty-front
npx tsgo -p tsconfig.json --noEmit
npx oxlint --type-aware -c .oxlintrc.json \
  src/branding/avity-brand.ts src/branding/__tests__/avity-brand.test.ts \
  src/utils/title-utils.ts src/utils/__tests__/title-utils.test.ts \
  src/modules/auth/components/Logo.tsx \
  src/modules/ui/utilities/page-title/components/PageTitle.tsx \
  src/modules/ui/navigation/navigation-drawer/constants/DefaultWorkspaceLogo.ts \
  src/loading/components/LeftPanelSkeletonLoader.tsx src/loading/components/PageContentSkeletonLoader.tsx \
  src/loading/components/UserOrMetadataLoader.tsx src/pages/auth/SignInUp.tsx src/index.tsx
npx oxfmt --check \
  src/branding src/utils/title-utils.ts src/utils/__tests__/title-utils.test.ts \
  src/modules/auth/components/Logo.tsx \
  src/modules/ui/utilities/page-title/components/PageTitle.tsx \
  src/modules/ui/navigation/navigation-drawer/constants/DefaultWorkspaceLogo.ts \
  src/loading/components/LeftPanelSkeletonLoader.tsx src/loading/components/PageContentSkeletonLoader.tsx \
  src/loading/components/UserOrMetadataLoader.tsx src/pages/auth/SignInUp.tsx src/index.tsx
npx jest src/utils/__tests__/title-utils.test.ts src/branding/__tests__/avity-brand.test.ts \
  --config=jest.config.mjs --runInBand
