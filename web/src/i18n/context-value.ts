import { createContext, useContext } from "react";

import { en } from "./en";
import type { Locale, Translations } from "./types";

export interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: Translations;
}

export const I18nContext = createContext<I18nContextValue>({
  locale: "en",
  setLocale: () => {},
  t: en,
});

export function useI18n(): I18nContextValue {
  return useContext(I18nContext);
}
