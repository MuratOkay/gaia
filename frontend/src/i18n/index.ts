// Every string the UI shows comes from a locale file (Phase 5 criterion: no hard-coded
// English outside locales). English only for now (D14); the keys are the contract.
import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./locales/en.json";

void i18n.use(initReactI18next).init({
  resources: { en: { translation: en } },
  lng: "en",
  fallbackLng: "en",
  interpolation: { escapeValue: false }, // React escapes already
  returnNull: false,
});

export default i18n;
