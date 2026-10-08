import { getAvityPageTitle } from '~/branding/avity-brand';

describe('Avity page titles', () => {
  it('keeps the translated page name with the product identity', () => {
    expect(getAvityPageTitle('Paramètres')).toBe('Paramètres · Avity-CRM');
  });

  it('does not repeat the product identity on the default page', () => {
    expect(getAvityPageTitle('Avity-CRM')).toBe('Avity-CRM');
  });
});
