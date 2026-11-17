import { createContext, useContext } from 'react'

export const CatalogContext = createContext(null)
export const useCatalog = () => useContext(CatalogContext)

// Тот же принцип, что и can_connect() на бэкенде: типы выхода и входа должны пересекаться.
export function canConnect(catalog, sourceType, targetType) {
  const src = catalog?.blocks[sourceType]
  const dst = catalog?.blocks[targetType]
  if (!src || !dst) return false
  return src.output.some((t) => dst.inputs.includes(t))
}
