import tailwindcssAnimate from 'tailwindcss-animate';
import tailwindPlugin from 'tailwindcss/plugin';
import type { Config } from 'tailwindcss';

/** Design token colors mapped to primitive CSS vars. */
export const ds = {
  gray: {
    lightest: 'var(--primitive-gray-50)',
    subtlest: 'var(--primitive-gray-100)',
    subtler: 'var(--primitive-gray-300)',
    subtle: 'var(--primitive-gray-500)',
    default: 'var(--primitive-gray-800)',
    darker: 'var(--primitive-gray-900)',
  },
  border: {
    subtlest: 'var(--primitive-gray-100)',
    subtler: 'var(--primitive-gray-200)',
    subtle: 'var(--primitive-gray-500)',
    default: 'var(--primitive-gray-800)',
    darker: 'var(--primitive-gray-900)',
  },
  warning: {
    // Semantic var, not a primitive: this rung is picked per mode. See the
    // note in src/styles/index.css.
    subtlest: 'var(--ds-warning-subtlest)',
    default: 'var(--primitive-orange-700)',
    darker: 'var(--primitive-orange-800)',
  },
  // Chart series palette — categorical identity slots, fixed order.
  // Use for legend swatches / inline series dots; charts read the CSS var
  // directly.
  series: {
    1: 'var(--ds-series-1)',
    2: 'var(--ds-series-2)',
    3: 'var(--ds-series-3)',
    4: 'var(--ds-series-4)',
    5: 'var(--ds-series-5)',
    6: 'var(--ds-series-6)',
    7: 'var(--ds-series-7)',
    8: 'var(--ds-series-8)',
    other: 'var(--ds-series-other)',
  },
  brand: {
    // Semantic var, not a primitive: picked per mode so a tinted surface stays
    // ABOVE the card it sits on in both themes. See the note in src/styles/index.css.
    lightest: 'var(--ds-brand-lightest)',
    subtlest: 'var(--primitive-brand-green-200)',
    // The filled chip and its ink. See the note in src/styles/index.css: the pair is
    // chosen for AA in BOTH modes.
    fill: 'var(--primitive-brand-green-400)',
    'on-fill': 'var(--primitive-brand-green-950)',
    subtler: 'var(--primitive-brand-green-500)',
    subtle: 'var(--primitive-brand-green-700)',
    default: 'var(--primitive-brand-green-800)',
    darker: 'var(--primitive-brand-green-900)',
  },
  text: {
    subtlest: 'var(--primitive-gray-100)',
    subtler: 'var(--primitive-gray-600)',
    subtle: 'var(--primitive-gray-700)',
    default: 'var(--primitive-gray-800)',
    darker: 'var(--primitive-gray-900)',
  },
  success: {
    // Points at the SEMANTIC var, not a primitive, because this rung is picked
    // per mode -- see the note on --ds-success-subtlest in src/styles/index.css. Same
    // shape as the `series` slots below.
    subtlest: 'var(--ds-success-subtlest)',
    subtler: 'var(--primitive-green-100)',
    default: 'var(--primitive-green-600)',
    darker: 'var(--primitive-green-900)',
  },
  danger: {
    subtlest: 'var(--primitive-red-100)',
    subtler: 'var(--primitive-red-300)',
    default: 'var(--primitive-red-600)',
    darker: 'var(--primitive-red-700)',
    darkest: 'var(--primitive-red-900)',
  },
  surface: {
    white: 'var(--primitive-white-solid)',
    'off-white': 'var(--primitive-white-off-white)',
  },
  purple: {
    subtlest: 'var(--ds-purple-subtlest)',
    default: 'var(--ds-purple-default)',
  },
  blue: {
    default: 'var(--ds-blue-default)',
  },
};


/** Tailwind v3 can't apply an opacity modifier (`bg-ds-surface-white/80`) to a
 * bare var() color and silently drops the class. Feeding it `<alpha-value>`
 * through color-mix makes `/NN` work; with no modifier it resolves to 100%.
 * Only the theme copy is wrapped; the exported `ds` stays raw var() strings. */
type ColorTree = { [k: string]: string | ColorTree };
const withAlpha = (tree: ColorTree): ColorTree =>
  Object.fromEntries(
    Object.entries(tree).map(([k, v]) => [
      k,
      typeof v === 'string' ? `color-mix(in srgb, ${v} calc(<alpha-value> * 100%), transparent)` : withAlpha(v),
    ])
  );

/** Design system typography: one utility per token (e.g. ds-text-body-xs-medium). */
const dsTypographyPlugin = function ({
  addUtilities,
}: {
  addUtilities: (utilities: Record<string, Record<string, string>>) => void;
}) {
  const bodyFont = 'var(--ds-font-body)';
  const codeFont = 'var(--ds-font-code)';
  addUtilities({
    '.ds-text-body-xs-medium': {
      fontSize: 'var(--ds-text-body-xs-medium)',
      fontFamily: bodyFont,
      fontWeight: '500',
      lineHeight: 'var(--ds-text-body-xs-medium-lh)',
    },
    '.ds-text-body-xs-semibold': {
      fontSize: 'var(--ds-text-body-xs-semibold)',
      fontFamily: bodyFont,
      fontWeight: '600',
      lineHeight: 'var(--ds-text-body-xs-semibold-lh)',
    },
    '.ds-text-body-sm-medium': {
      fontSize: 'var(--ds-text-body-sm-medium)',
      fontFamily: bodyFont,
      fontWeight: '500',
      lineHeight: 'var(--ds-text-body-sm-medium-lh)',
    },
    '.ds-text-body-sm-semibold': {
      fontSize: 'var(--ds-text-body-sm-semibold)',
      fontFamily: bodyFont,
      fontWeight: '600',
      lineHeight: 'var(--ds-text-body-sm-semibold-lh)',
    },
    '.ds-text-body-md-medium': {
      fontFamily: bodyFont,
      fontWeight: '500',
    },
    '.ds-text-body-md-semibold': {
      fontFamily: bodyFont,
      fontWeight: '600',
    },
    '.ds-text-title-sm-medium': {
      fontFamily: bodyFont,
      fontWeight: '500',
    },
    '.ds-text-title-sm-semibold': {
      fontSize: 'var(--ds-text-title-sm-semibold)',
      fontFamily: bodyFont,
      fontWeight: '600',
      lineHeight: 'var(--ds-text-title-sm-semibold-lh)',
    },
    '.ds-text-title-md-medium': {
      fontFamily: bodyFont,
      fontWeight: '500',
    },
    '.ds-text-title-md-semibold': {
      fontSize: 'var(--ds-text-title-md-semibold)',
      fontFamily: bodyFont,
      fontWeight: '600',
      lineHeight: 'var(--ds-text-title-md-semibold-lh)',
    },
    '.ds-text-title-lg-medium': {
      fontFamily: bodyFont,
      fontWeight: '500',
    },
    '.ds-text-title-lg-semibold': {
      fontFamily: bodyFont,
      fontWeight: '600',
    },
    '.ds-text-title-xl-medium': {
      fontFamily: bodyFont,
      fontWeight: '500',
    },
    '.ds-text-title-xl-semibold': {
      fontFamily: bodyFont,
      fontWeight: '600',
    },
    '.ds-text-title-2xl-medium': {
      fontFamily: bodyFont,
      fontWeight: '500',
    },
    '.ds-text-title-2xl-semibold': {
      fontFamily: bodyFont,
      fontWeight: '600',
    },
    '.ds-text-code-sm': {
      fontFamily: codeFont,
      fontWeight: '500',
    },
    '.ds-text-code-md': {
      fontFamily: codeFont,
      fontWeight: '500',
    },
    '.ds-text-code-md-regular': {
      fontFamily: codeFont,
      fontWeight: '400',
    },
    '.ds-text-code-lg': {
      fontFamily: codeFont,
      fontWeight: '700',
    },
  });
};

const config: Config = {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        code: ['var(--font-jetbrains-mono)', 'ui-monospace', 'monospace'],
      },
      transitionTimingFunction: {
        'motion-out': 'var(--motion-ease-out)',
      },
      colors: {
        muted: 'hsl(var(--muted))',
        border: 'hsl(var(--border))',
        ds: withAlpha(ds),
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
      },
      keyframes: {
        // The Review tab: the one control here that is an offer rather than
        // a view of the session, so it breathes instead of sitting still.
        // Slow and small on purpose -- it shares a 300px column with a map
        // and a timeline, and anything faster reads as an alert.
        // Both ends are lit: it travels between a visible glow and a
        // brighter one, never through transparent. A pulse that fades out
        // spends half its cycle looking like a control nobody styled.
        'pulse-glow': {
          '0%, 100%': {
            boxShadow:
              '0 0 0 1px var(--primitive-brand-green-500), 0 1px 6px -1px var(--primitive-brand-green-500)',
            transform: 'translateY(0)',
          },
          '50%': {
            boxShadow:
              '0 0 0 1.5px var(--primitive-brand-green-400), 0 5px 18px -3px var(--primitive-brand-green-400)',
            transform: 'translateY(-1.5px)',
          },
        },
      },
      animation: {
        // A literal curve, not a var(): an unresolved var() invalidates the
        // whole shorthand, which silently leaves `animation: none`.
        'pulse-glow': 'pulse-glow 2.8s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
    },
  },
  plugins: [
    tailwindcssAnimate,
    dsTypographyPlugin,
    // Base UI (@base-ui/react) exposes state as boolean-presence data-attributes
    // (data-open, data-checked, data-starting-style, ...). Register them as
    // shorthand Tailwind variants so `data-open:`, `group-data-active:`, etc.
    // resolve. Parameterized attrs (data-[side=top], data-[state=...]) use the
    // native bracket form and need no registration.
    tailwindPlugin(({ addVariant }: { addVariant: (name: string, selector: string) => void }) => {
      const states = [
        'open', 'closed', 'checked', 'unchecked', 'indeterminate',
        'disabled', 'readonly', 'required', 'valid', 'invalid', 'dirty',
        'touched', 'filled', 'focused', 'active', 'inactive', 'selected',
        'unselected', 'highlighted', 'pressed', 'current', 'popup-open',
        'starting-style', 'ending-style', 'scrolling', 'nested',
      ];
      for (const s of states) {
        addVariant(`data-${s}`, `&[data-${s}]`);
        addVariant(`group-data-${s}`, `:merge(.group)[data-${s}] &`);
        addVariant(`peer-data-${s}`, `:merge(.peer)[data-${s}] ~ &`);
      }
    }),
  ],
};
export default config;
