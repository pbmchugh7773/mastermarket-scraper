# PRICE_PROMOTION_LOGIC.md

## Documentacion Completa: Logica de Precios y Promociones

Este documento describe la arquitectura de precios y promociones utilizada por el scraper de MasterMarket, para asegurar consistencia entre el scraper, backend y frontend.

---

## 1. REGLA PRINCIPAL

```
price           = Precio PROMOCIONAL (lo que paga el cliente)
original_price  = Precio SIN promocion (precio regular/normal)
```

Esta regla aplica a TODAS las tiendas para mantener consistencia.

---

## 2. ARQUITECTURA DE PRECIOS

### 2.1 Flujo de Datos

```
SCRAPER                         BACKEND                          FRONTEND
───────                         ───────                          ────────
scrape_*()                      POST /api/community-prices       BasketContext
    │                           /submit-scraped                  calculateOptimalPurchase()
    ▼                                │                                │
detect_*_promotion_data()            ▼                                ▼
    │                           CommunityPrice ─────────────► price comparison
    ▼                           LatestPrice                    basket totals
upload_price()                                                 store optimization
```

### 2.2 Campos del Modelo CommunityPrice

| Campo | Tipo | Descripcion | Ejemplo |
|-------|------|-------------|---------|
| `price` | Float | **Precio que paga el cliente** (promocional si aplica) | 2.00 |
| `original_price` | Float | Precio sin promocion/descuento | 3.00 |
| `promotion_type` | String | Tipo de promocion | `membership_price` |
| `promotion_text` | String | Texto legible de la promocion | "Only €2.00 Real Rewards" |
| `promotion_discount_value` | Float | Valor del ahorro en EUR | 1.00 |
| `promotion_expires_at` | DateTime | Fecha de expiracion (si aplica) | null |

---

## 3. LOGICA POR TIENDA

### 3.1 TESCO (Clubcard)

**Modelo**: Membresia (Clubcard vs Regular)

**Comportamiento**:
- Los productos con Clubcard tienen DOS precios en la pagina
- Precio Clubcard: precio reducido para miembros
- Precio Regular: precio para no miembros

**Logica del Scraper** (`scrape_tesco()`):
```python
# Extrae ambos precios del HTML
clubcard_price = extract_clubcard_price()  # ej: €2.00
regular_price = extract_regular_price()     # ej: €3.00

# Retorna:
price = clubcard_price  # €2.00 - lo que paga el cliente (miembro)
original_price = regular_price  # €3.00 - precio sin membresia
promotion_type = "membership_price"
promotion_text = "Clubcard Price"
promotion_discount_value = regular_price - clubcard_price  # €1.00
```

**Ejemplo Real**:
```
Producto: Dolmio Sauce
URL: https://www.tesco.ie/groceries/product/details/12345

price: 2.00
original_price: 3.00
promotion_type: membership_price
promotion_text: Clubcard Price
promotion_discount_value: 1.00
```

---

### 3.2 SUPERVALU (Real Rewards)

**Modelo**: Membresia (Real Rewards vs Regular)

**Comportamiento**:
- Similar a Tesco pero con programa "Real Rewards"
- El badge dice "Only €X.XX Real Rewards Price"
- El texto indica "non-Real Rewards members will pay €Y"

**Logica del Scraper** (`_scrape_supervalu_requests_fallback()` + `detect_supervalu_promotion_data()`):
```python
# Patrones para extraer precio Real Rewards
real_rewards_patterns = [
    r'Only\s*€\s*(\d+[.,]\d{2})\s*Real\s*Rewards\s*Price',
    r'Real\s*Rewards\s*members\s*will\s*pay\s*€\s*(\d+[.,]\d{2})',
]

# Patrones para extraer precio normal (no miembros)
normal_price_patterns = [
    r'non-Real\s*Rewards\s*members\s*will\s*pay\s*€\s*(\d+[.,]?\d*)',
]

# Si hay Real Rewards:
price = real_rewards_price  # €2.00 - precio para miembros
original_price = normal_price  # €3.00 - precio sin membresia
promotion_type = "membership_price"
promotion_text = "Only €2.00 Real Rewards Price"
promotion_discount_value = normal_price - real_rewards_price  # €1.00
```

**Ejemplo Real**:
```
Producto: Dolmio Original Bolognese Sauce 450g
URL: https://shop.supervalu.ie/sm/delivery/rsid/5550/product/dolmio-original-bolognese-sauce-450-g-id-1026540002

price: 2.00
original_price: 3.00
promotion_type: membership_price
promotion_text: Only €2.00 Real Rewards Price
promotion_discount_value: 1.00
```

**IMPORTANTE**: SuperValu tambien puede tener "Was price" (precio anterior), pero este NO se usa como `original_price` cuando hay Real Rewards. El `original_price` siempre es el precio para no-miembros.

---

### 3.3 ALDI (Was/Now, Super Saver, Special Buy)

**Modelo**: Sin membresia - Todos pagan el mismo precio

**Comportamiento**:
- El precio visible en la web ES el precio para todos
- Las promociones son tipo "Was €X → Now €Y"
- No hay distincion entre miembros y no miembros

**Logica del Scraper** (`scrape_aldi()` + `detect_aldi_promotion_data()`):
```python
# Extrae el precio visible (ya es el precio promocional)
current_price = extract_price_from_page()  # €2.00

# Busca "Was price" para calcular descuento
was_patterns = [
    r'was[:\s]*[€£]?\s*(\d+[.,]\d{2})',
    r'original[:\s]*price[:\s]*[€£]?\s*(\d+[.,]\d{2})',
]

# Si hay "Was price":
price = current_price  # €2.00 - precio actual
original_price = was_price  # €3.50 - precio anterior
promotion_type = "temporary_discount"
promotion_text = "Was €3.50"
promotion_discount_value = was_price - current_price  # €1.50
```

**Tipos de Promocion Aldi**:
| Tipo | promotion_type | Descripcion |
|------|----------------|-------------|
| Was/Now | `temporary_discount` | Precio reducido temporalmente |
| Super Saver | `clearance` | Ofertas especiales tipo liquidacion |
| Special Buy | `flash_sale` | Ofertas por tiempo limitado |
| X% Off | `percentage_off` | Descuento porcentual |
| Save €X | `fixed_amount_off` | Descuento fijo en euros |
| 3 for €5 | `multi_buy` | Ofertas multi-compra |

**Ejemplo Real**:
```
Producto: Nutella Hazelnut Spread
URL: https://www.aldi.ie/product/nutella-hazelnut-spread-000000000000337161

price: 4.29
original_price: null  (no hay promocion en este producto)
promotion_type: null
promotion_text: null
```

---

### 3.4 DUNNES (Estado Actual)

**Modelo**: Sin deteccion de promociones implementada

**Comportamiento Actual**:
- Solo se extrae el precio visible
- No se detectan promociones

**Logica del Scraper** (`scrape_dunnes()`):
```python
price = extract_price()  # Precio visible
promotion_data = None    # No se detectan promociones
```

**TODO Futuro**: Implementar deteccion de promociones similar a las otras tiendas.

---

### 3.5 LIDL (Estado Actual)

**Modelo**: Sin deteccion de promociones implementada

**Comportamiento Actual**:
- Solo se extrae el precio visible
- No se detectan promociones

**Logica del Scraper** (`scrape_lidl()`):
```python
price = extract_price()  # Precio visible
promotion_data = None    # No se detectan promociones
```

**TODO Futuro**: Implementar deteccion de promociones similar a las otras tiendas.

---

## 4. TIPOS DE PROMOCION SOPORTADOS

| Tipo | Codigo | Tiendas | Descripcion |
|------|--------|---------|-------------|
| Precio Membresia | `membership_price` | Tesco, SuperValu | Clubcard, Real Rewards |
| Descuento Temporal | `temporary_discount` | Aldi | Was/Now pricing |
| Multi-compra | `multi_buy` | Todas | "3 for €5", "Buy 2 Get 1" |
| Porcentaje | `percentage_off` | Aldi | "25% Off" |
| Monto Fijo | `fixed_amount_off` | Aldi | "Save €2" |
| Liquidacion | `clearance` | Aldi | Super Saver |
| Oferta Flash | `flash_sale` | Aldi | Special Buy |

---

## 5. PAYLOAD ENVIADO AL BACKEND

### 5.1 Funcion upload_price()

```python
def upload_price(self, product_id: int, price: float, store: str, promotion_data: dict = None):
    payload = {
        "product_id": product_id,
        "price": price,  # SIEMPRE el precio promocional
        "store_id": STORE_MAPPING[store],
        "is_scraped": True,
        "country": self.country,

        # Campos de promocion (opcionales)
        "original_price": promotion_data.get('original_price') if promotion_data else None,
        "promotion_type": promotion_data.get('promotion_type') if promotion_data else None,
        "promotion_text": promotion_data.get('promotion_text') if promotion_data else None,
        "promotion_discount_value": promotion_data.get('promotion_discount_value') if promotion_data else None,
        "promotion_expires_at": promotion_data.get('promotion_expires_at') if promotion_data else None,
    }

    requests.post(f"{API_URL}/api/community-prices/submit-scraped", json=payload)
```

### 5.2 Ejemplos de Payloads

**Tesco con Clubcard**:
```json
{
  "product_id": 123,
  "price": 2.00,
  "store_id": 1,
  "is_scraped": true,
  "country": "IE",
  "original_price": 3.00,
  "promotion_type": "membership_price",
  "promotion_text": "Clubcard Price",
  "promotion_discount_value": 1.00,
  "promotion_expires_at": null
}
```

**SuperValu con Real Rewards**:
```json
{
  "product_id": 456,
  "price": 2.00,
  "store_id": 2,
  "is_scraped": true,
  "country": "IE",
  "original_price": 3.00,
  "promotion_type": "membership_price",
  "promotion_text": "Only €2.00 Real Rewards Price",
  "promotion_discount_value": 1.00,
  "promotion_expires_at": null
}
```

**Aldi con Was/Now**:
```json
{
  "product_id": 789,
  "price": 2.00,
  "store_id": 3,
  "is_scraped": true,
  "country": "IE",
  "original_price": 3.50,
  "promotion_type": "temporary_discount",
  "promotion_text": "Was €3.50",
  "promotion_discount_value": 1.50,
  "promotion_expires_at": null
}
```

**Producto sin promocion**:
```json
{
  "product_id": 101,
  "price": 4.29,
  "store_id": 3,
  "is_scraped": true,
  "country": "IE",
  "original_price": null,
  "promotion_type": null,
  "promotion_text": null,
  "promotion_discount_value": null,
  "promotion_expires_at": null
}
```

---

## 6. GUIA PARA BACKEND Y FRONTEND

### 6.1 Mostrar Precios en la App

```typescript
// Mostrar precio principal (lo que paga el cliente)
const displayPrice = communityPrice.price;  // €2.00

// Mostrar precio original (tachado) si hay promocion
const showOriginalPrice = communityPrice.original_price &&
                          communityPrice.original_price > communityPrice.price;

// Ejemplo de UI
<View>
  {showOriginalPrice && (
    <Text style={styles.originalPrice}>€{original_price.toFixed(2)}</Text>  // €3.00 tachado
  )}
  <Text style={styles.price}>€{displayPrice.toFixed(2)}</Text>  // €2.00
  {promotion_text && (
    <Badge>{promotion_text}</Badge>  // "Only €2.00 Real Rewards Price"
  )}
</View>
```

### 6.2 Calcular Canasta CON Promociones (Default)

```typescript
// Lo que el cliente paga si aprovecha todas las promociones
const totalWithPromotions = basket.items.reduce((sum, item) => {
  return sum + (item.price * item.quantity);
}, 0);
```

### 6.3 Calcular Canasta SIN Promociones

```typescript
// Lo que el cliente pagaria sin ninguna promocion
const totalWithoutPromotions = basket.items.reduce((sum, item) => {
  const effectivePrice = item.original_price || item.price;
  return sum + (effectivePrice * item.quantity);
}, 0);
```

### 6.4 Calcular Ahorro Total

```typescript
const savings = totalWithoutPromotions - totalWithPromotions;
// Ejemplo: €30.00 - €24.00 = €6.00 de ahorro
```

### 6.5 Optimizar Compra Multi-Tienda

```typescript
function calculateOptimalPurchase(items, includePromotions = true) {
  // Para cada producto, encontrar la tienda con el mejor precio
  return items.map(item => {
    const prices = item.storePrices.map(sp => ({
      store: sp.store,
      // Usar precio con o sin promocion segun preferencia
      price: includePromotions ? sp.price : (sp.original_price || sp.price)
    }));

    // Ordenar por precio y seleccionar el mas barato
    prices.sort((a, b) => a.price - b.price);
    return { ...item, bestStore: prices[0] };
  });
}
```

### 6.6 Filtrar por Tipo de Promocion

```typescript
// Obtener solo productos con promociones de membresia
const membershipDeals = products.filter(p =>
  p.promotion_type === 'membership_price'
);

// Obtener solo productos con descuentos temporales
const tempDiscounts = products.filter(p =>
  p.promotion_type === 'temporary_discount'
);

// Obtener todos los productos en promocion
const allDeals = products.filter(p =>
  p.promotion_type !== null
);
```

---

## 7. VALIDACIONES IMPORTANTES

### 7.1 Validacion de Precios

```python
# El scraper valida:
if price is None or price <= 0:
    return (None, None)  # No guardar precio invalido

if price > 1000:
    logger.warning(f"Precio sospechosamente alto: €{price}")
    return (None, None)

# original_price debe ser mayor que price
if original_price and original_price <= price:
    original_price = None  # Ignorar original_price invalido
```

### 7.2 Validacion de Promociones

```python
# No confundir precio por kilo con promociones
EXCLUDED_PATTERNS = [
    r'/kg', r'/100g', r'/litre', r'/each',
    r'per\s*kg', r'per\s*100g'
]

# No confundir dimensiones con multi-buy
if pattern matches "32x32" or "144x144":
    skip  # Son dimensiones de imagen, no promociones
```

---

## 8. ARCHIVOS RELEVANTES

| Archivo | Ubicacion | Proposito |
|---------|-----------|-----------|
| `simple_local_to_prod.py` | `/mastermarket-scraper/` | Scraper principal con toda la logica |
| `schemas.py` | `/backend/app/` | Definicion de CommunityPriceCreate |
| `community_prices.py` | `/backend/app/api/routes/` | Endpoint POST /submit-scraped |
| `BasketContext.tsx` | `/web/src/context/` | Logica de canasta en frontend |

---

## 9. HISTORIAL DE CAMBIOS

| Fecha | Cambio | Afecta |
|-------|--------|--------|
| 2025-01-05 | SuperValu: Real Rewards price como `price` principal | SuperValu scraper |
| 2025-01-05 | Aldi: Validacion de patron NxM (evitar dimensiones) | Aldi scraper |
| 2025-01-05 | SuperValu: Filtros para precio por kilo | SuperValu scraper |

---

## 10. RESUMEN EJECUTIVO

```
╔════════════════════════════════════════════════════════════════════╗
║  REGLA UNICA: price = precio promocional, original_price = normal ║
╚════════════════════════════════════════════════════════════════════╝

TESCO:     price = Clubcard    │ original_price = Regular
SUPERVALU: price = Real Rewards│ original_price = No-member price
ALDI:      price = Current     │ original_price = Was price
DUNNES:    price = Visible     │ original_price = null (no detectado)
LIDL:      price = Visible     │ original_price = null (no detectado)
```

---

*Ultima actualizacion: 2025-01-05*
*Mantenido por: MasterMarket Scraper Team*
