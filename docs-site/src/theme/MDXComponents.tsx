import MDXComponents from '@theme-original/MDXComponents';
import ScrollableTable from '@site/src/components/ScrollableTable';

// Every markdown table renders inside a horizontal scroll container so wide tables
// stay readable on phones instead of being clipped or forcing the page sideways.
export default {
  ...MDXComponents,
  table: ScrollableTable,
};
