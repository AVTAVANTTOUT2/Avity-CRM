import { getAvityPageTitle } from '~/branding/avity-brand';
import { Helmet } from '@dr.pogodin/react-helmet';

import { useWorkspaceSurface } from '@/ui/layout/hooks/useWorkspaceSurface';

type PageTitleProps = {
  title: string;
};

export const PageTitle = (props: PageTitleProps) => {
  const workspaceSurface = useWorkspaceSurface();

  if (workspaceSurface.type === 'side-panel') {
    return null;
  }

  return (
    <Helmet>
      <title>{getAvityPageTitle(props.title)}</title>
    </Helmet>
  );
};
