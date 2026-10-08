import { styled } from '@linaria/react';

import { NAVIGATION_DRAWER_CONSTRAINTS } from '@/ui/layout/resizable-panel/constants/NavigationDrawerConstraints';
import { MOBILE_VIEWPORT, themeCssVariables } from 'twenty-ui/theme';
import { useIsMobile } from 'twenty-ui/utilities';
import { AVITY_BRAND_ICON, AVITY_BRAND_NAME } from '~/branding/avity-brand';
import { LeftPanelSkeletonLoader } from '~/loading/components/LeftPanelSkeletonLoader';
import { PageContentSkeletonLoader } from '~/loading/components/PageContentSkeletonLoader';

const StyledContainer = styled.div`
  background: ${themeCssVariables.background.tertiary};
  box-sizing: border-box;
  display: flex;
  flex-direction: row;
  height: calc(100dvh / var(--t-zoom, 1));
  min-width: ${NAVIGATION_DRAWER_CONSTRAINTS.default}px;
  overflow: hidden;
  width: 100%;

  @media (max-width: ${MOBILE_VIEWPORT}px) {
    width: 100%;
  }
`;

const StyledLeftPanelWrapper = styled.div`
  flex-shrink: 0;
`;

export const UserOrMetadataLoader = () => {
  const isMobile = useIsMobile();
  return (
    <StyledContainer>
      <StyledLeftPanelWrapper>
        <LeftPanelSkeletonLoader />
      </StyledLeftPanelWrapper>
      <PageContentSkeletonLoader
        headerIcon={
          isMobile ? (
            <img src={AVITY_BRAND_ICON} width={20} height={20} alt="" />
          ) : undefined
        }
        headerTitle={isMobile ? AVITY_BRAND_NAME : undefined}
      />
    </StyledContainer>
  );
};
