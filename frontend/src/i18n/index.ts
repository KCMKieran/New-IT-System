// i18n configuration and type definitions
import { zhCN } from "./locales/zh-CN"
import { enUS } from "./locales/en-US"

// Supported languages
export type Language = "zh-CN" | "en-US"

// Recursively widen literal string types to `string` while preserving key structure
type DeepStringRecord<T> = {
  readonly [K in keyof T]: T[K] extends string ? string : DeepStringRecord<T[K]>
}

// Translation dictionary keeps zhCN's nested key structure but accepts any string values
export type Translations = DeepStringRecord<typeof zhCN>

// All translations mapped by language code
export const translations: Record<Language, Translations> = {
  "zh-CN": zhCN,
  "en-US": enUS,
}

// Default language
export const defaultLanguage: Language = "zh-CN"

// Get translation function type
export type TFunction = (key: string) => string

// Helper function to get nested translation value
function getNestedValue(obj: any, path: string): string {
  const keys = path.split(".")
  let value = obj
  for (const key of keys) {
    if (value && typeof value === "object" && key in value) {
      value = value[key]
    } else {
      return path // Return key if not found
    }
  }
  return typeof value === "string" ? value : path
}

// Helper function to replace placeholders in translation strings
function replacePlaceholders(text: string, params?: Record<string, string | number>): string {
  if (!params) return text
  let result = text
  for (const [key, value] of Object.entries(params)) {
    result = result.replace(new RegExp(`\\{${key}\\}`, 'g'), String(value))
  }
  return result
}

// Enhanced translation function type that supports parameters
export type TFunctionWithParams = (key: string, params?: Record<string, string | number>) => string

// Create translation function with placeholder support
export function createT(lang: Language): TFunctionWithParams {
  const t = translations[lang] || translations[defaultLanguage]
  return (key: string, params?: Record<string, string | number>) => {
    const value = getNestedValue(t, key)
    return replacePlaceholders(value, params)
  }
}

