import type { Preview } from "@storybook/react-vite";

import "../src/global.css";

const preview: Preview = {
  parameters: {
    a11y: {
      test: "error",
    },
    controls: {
      expanded: true,
    },
    layout: "centered",
  },
  tags: ["autodocs"],
};

export default preview;
