export const AVITY_BRAND_NAME = 'Avity-CRM';
export const AVITY_BRAND_ICON = '/images/avity/wordmark.svg';

export const getAvityPageTitle = (title: string): string =>
  title === AVITY_BRAND_NAME ? title : `${title} · ${AVITY_BRAND_NAME}`;
