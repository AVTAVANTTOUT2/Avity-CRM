import { AVITY_BRAND_ICON } from '~/branding/avity-brand';

export const DEFAULT_WORKSPACE_LOGO = new URL(
  AVITY_BRAND_ICON,
  window.location.origin,
).href;
