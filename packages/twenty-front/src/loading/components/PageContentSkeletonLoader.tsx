import { SKELETON_LOADER_HEIGHT_SIZES } from '@/activities/components/SkeletonLoader';
import { PageCardHeader } from '@/ui/layout/page/components/PageCardHeader';
import { PageCardLayout } from '@/ui/layout/page/components/PageCardLayout';
import { type ReactNode } from 'react';
import Skeleton, { SkeletonTheme } from 'react-loading-skeleton';
import { useTheme } from 'twenty-ui/theme';

type PageContentSkeletonLoaderProps = {
  headerIcon?: ReactNode;
  headerTitle?: ReactNode;
  secondaryBar?: ReactNode;
};

export const PageContentSkeletonLoader = ({
  headerIcon,
  headerTitle,
  secondaryBar,
}: PageContentSkeletonLoaderProps) => {
  const theme = useTheme();

  return (
    <SkeletonTheme
      baseColor={theme.background.tertiary}
      highlightColor={theme.background.transparent.lighter}
      borderRadius={4}
    >
      <PageCardLayout
        header={
          <PageCardHeader
            icon={headerIcon ?? <Skeleton width={20} height={20} />}
            title={
              headerTitle ?? (
                <Skeleton
                  width={120}
                  height={SKELETON_LOADER_HEIGHT_SIZES.standard.s}
                />
              )
            }
            actionButton={
              <Skeleton
                width={80}
                height={SKELETON_LOADER_HEIGHT_SIZES.standard.s}
              />
            }
          />
        }
        secondaryBar={secondaryBar}
        showInformationBanner={false}
      >
        {null}
      </PageCardLayout>
    </SkeletonTheme>
  );
};
