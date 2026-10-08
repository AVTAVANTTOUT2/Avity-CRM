import { getAvityPageTitle } from '~/branding/avity-brand';
import { DEFAULT_WORKSPACE_LOGO } from '@/ui/navigation/navigation-drawer/constants/DefaultWorkspaceLogo';
import { getImageAbsoluteURI } from 'twenty-shared/utils';

describe('Avity page titles', () => {
  it('keeps the translated page name with the product identity', () => {
    expect(getAvityPageTitle('Paramètres')).toBe('Paramètres · Avity-CRM');
  });

  it('does not repeat the product identity on the default page', () => {
    expect(getAvityPageTitle('Avity-CRM')).toBe('Avity-CRM');
  });
});

describe('Avity default workspace logo', () => {
  it('keeps the public asset URL when the uploaded image resolver receives it', () => {
    expect(
      getImageAbsoluteURI({
        imageUrl: DEFAULT_WORKSPACE_LOGO,
        baseUrl: 'https://files.example.invalid',
      }),
    ).toBe(new URL('/images/avity/wordmark.svg', window.location.origin).href);
  });
});
