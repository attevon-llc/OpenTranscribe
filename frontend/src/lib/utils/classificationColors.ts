/**
 * Classification banner colours per MIL-STD-129 / DoDM 5200.01 / NARA CUI marking. Regulatory data,
 * not theme tokens: the colour of a marking is fixed by the standard in both themes. Shared by
 * ClassificationBanner and LoginBanner so there is one palette; `classificationColors.test.ts`
 * pins AA (4.5:1) for every text/background pair.
 */
export interface ClassificationColor {
  bg: string;
  text: string;
}

export const unclassifiedColors: ClassificationColor = { bg: '#007a33', text: '#ffffff' };

export const classificationColors: Record<string, ClassificationColor> = {
  UNCLASSIFIED: unclassifiedColors,
  CUI: { bg: '#502b85', text: '#ffffff' },
  FOUO: { bg: '#ffc72c', text: '#000000' },
  CONFIDENTIAL: { bg: '#0033a0', text: '#ffffff' },
  SECRET: { bg: '#c8102e', text: '#ffffff' },
  'TOP SECRET': { bg: '#ff671f', text: '#000000' },
  'TOP SECRET//SCI': { bg: '#fce300', text: '#000000' },
};

export function colorsFor(classification: string): ClassificationColor {
  return classificationColors[classification] ?? unclassifiedColors;
}
