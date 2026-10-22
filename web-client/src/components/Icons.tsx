import type { JSX } from 'react';

type IconProps = { size?: number; className?: string };

function svg(path: JSX.Element, { size = 16, className }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.3}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {path}
    </svg>
  );
}

export const IconSearch = (props: IconProps) =>
  svg(
    <>
      <circle cx="7" cy="7" r="4.3" />
      <path d="M10.3 10.3 13.5 13.5" />
    </>,
    props,
  );

export const IconLibrary = (props: IconProps) =>
  svg(
    <>
      <path d="M2.5 3.2h4.2v9.6H2.5z" />
      <path d="M7.6 3.2h5.9v9.6H7.6z" />
    </>,
    props,
  );

export const IconJobs = (props: IconProps) =>
  svg(
    <>
      <path d="M2.5 5.5v-3h3" />
      <path d="M13.5 5.5v-3h-3" />
      <path d="M2.5 10.5v3h3" />
      <path d="M13.5 10.5v3h-3" />
      <path d="M2.5 8h11" />
    </>,
    props,
  );

export const IconExcerpts = (props: IconProps) =>
  svg(
    <>
      <path d="M4 3.2h3.2v3.2H4z" />
      <path d="M9 3.2h3.2v3.2H9z" />
      <path d="M2.8 9.6h10.4" />
      <path d="M2.8 12.4h7" />
    </>,
    props,
  );

export const IconProjects = (props: IconProps) =>
  svg(
    <>
      <path d="M8 2.4 14 5.4 8 8.4 2 5.4z" />
      <path d="M2.4 8.6 8 11.4l5.6-2.8" />
    </>,
    props,
  );

export const IconSettings = (props: IconProps) =>
  svg(
    <>
      <path d="M2.5 4.6h11" />
      <path d="M2.5 11.4h11" />
      <circle cx="6" cy="4.6" r="1.6" />
      <circle cx="10.6" cy="11.4" r="1.6" />
    </>,
    props,
  );

export const IconMenu = (props: IconProps) =>
  svg(
    <path d="M2 4h12M2 8h12M2 12h12" />,
    props,
  );

export const IconChevron = (props: IconProps) =>
  svg(<path d="M3 4.5 6 7.5 9 4.5" />, props);

export const IconInfo = (props: IconProps) =>
  svg(
    <>
      <circle cx="8" cy="8" r="6.2" />
      <path d="M8 7.2v4M8 5.1v.1" />
    </>,
    props,
  );

export const IconUpload = (props: IconProps) =>
  svg(
    <>
      <path d="M8 11V2.6" />
      <path d="M4.8 5.6 8 2.4l3.2 3.2" />
      <path d="M2.6 10.4v1.8a1.2 1.2 0 0 0 1.2 1.2h8.4a1.2 1.2 0 0 0 1.2-1.2v-1.8" />
    </>,
    props,
  );

export const IconDownload = (props: IconProps) =>
  svg(
    <>
      <path d="M8 2.4V10.8" />
      <path d="M4.8 7.6 8 10.8l3.2-3.2" />
      <path d="M2.6 12.4v.4a1 1 0 0 0 1 1h8.8a1 1 0 0 0 1-1v-.4" />
    </>,
    props,
  );