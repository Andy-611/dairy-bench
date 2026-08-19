import type { DetailedHTMLProps, HTMLAttributes } from "react";

type MathMLIntrinsicElements = {
  [Tag in keyof MathMLElementTagNameMap]: DetailedHTMLProps<
    HTMLAttributes<MathMLElementTagNameMap[Tag]>,
    MathMLElementTagNameMap[Tag]
  >;
};

declare module "react" {
  namespace JSX {
    interface IntrinsicElements extends MathMLIntrinsicElements {}
  }
}
