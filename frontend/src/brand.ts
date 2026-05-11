// Domain-based branding.
// Add new brands by extending the BRANDS map.

export type Brand = {
  name: string;       // shown in header / login / <title>
  logo: string;       // 1x logo path
  logo2x: string;     // 2x logo path (high-DPI)
  favicon: string;    // favicon path
  alt: string;        // image alt text
};

const BRANDS: Record<string, Brand> = {
  'bots.smartb.co': {
    name: 'SmartB Agents',
    logo: '/brands/smartb/logo.png',
    logo2x: '/brands/smartb/logo@2x.png',
    favicon: '/brands/smartb/logo.png',
    alt: 'SmartB',
  },
};

const DEFAULT_BRAND: Brand = {
  name: 'Netforce Agents',
  logo: '/logo.png',
  logo2x: '/logo@2x.png',
  favicon: '/logo.png',
  alt: 'Netforce',
};

export function getBrand(): Brand {
  if (typeof window === 'undefined') return DEFAULT_BRAND;
  return BRANDS[window.location.hostname] ?? DEFAULT_BRAND;
}

// Side-effect: keep <title> and <link rel="icon"> in sync with the brand.
export function applyDocumentBrand(): void {
  if (typeof document === 'undefined') return;
  const b = getBrand();
  document.title = b.name;
  let icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
  if (!icon) {
    icon = document.createElement('link');
    icon.rel = 'icon';
    document.head.appendChild(icon);
  }
  icon.href = b.favicon;
}
