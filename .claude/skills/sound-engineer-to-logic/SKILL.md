---
name: sound-engineer-to-logic
description: Aplica en Logic Pro las recetas de mezcla y mastering que produce el MCP mcp_sound_engineer (judge_mix, judge_master), usando el MCP mcp_logic (logic_mixer, logic_plugins, logic_project, etc.). Úsalo siempre que el usuario pida mezclar, masterizar, evaluar o "aplicar la receta" sobre un proyecto de Logic, o mencione sound engineer + Logic Pro, recetas de EQ/compresor/limiter, o cerrar el ciclo medir → aplicar → bounce → volver a medir. Úsalo aunque el usuario solo diga "mastériza esto en Logic" o "arréglame esta mezcla".
---

# Sound Engineer → Logic Pro

Dos MCPs independientes:

- **mcp_sound_engineer** mide y decide. Devuelve una *receta*: lista ordenada de acciones neutrales al DAW (`eq.high_pass`, `eq.bell`, `compressor`, `limiter`, `fader`, `dither`, `send_back`...).
- **mcp_logic** controla Logic Pro (macOS). Tools: `logic_mixer`, `logic_plugins`, `logic_tracks`, `logic_project`, `logic_audio`, `logic_system`, etc.

Este skill es el adaptador: traduce la receta a llamadas de Logic, de forma honesta sobre lo que se pudo verificar.

## Reglas que no se rompen

1. **`send_back` es terminal.** Si la receta contiene `send_back`, no toques Logic. Reporta los `reasons` al usuario y para.
2. **No inventes parámetros.** No conozco de memoria los nombres exactos de los argumentos de las tools de Logic. Antes de la primera llamada de cada tool, lee su schema, o el catálogo de operaciones (resource `logic://` de operation catalog) o `docs/API.md` del repo de mcp_logic. Si no puedes confirmar un parámetro, no lo adivines.
3. **Respeta los estados de mcp_logic.** Cada operación que muta devuelve A (confirmado), B (incierto) o C (fallo). Con B, lee el estado real (`logic://mixer`, `logic://tracks`) antes de reintentar. Con C, no reintentes a ciegas.
4. **Nunca superes los topes de la receta.** EQ correctivo ≤ 3 dB por punto y ≤ 6 dB acumulado, gain reduction del compresor ≤ 3 dB, limiter ≤ 4 dB, fader ±3 dB. Si una acción los excede, aplícala recortada y repórtalo.
5. **Confirma con el usuario** antes de insertar plugins o de cualquier operación destructiva del proyecto.
6. **Un dato no verificado se reporta como no verificado.** Nunca digas "aplicado" si solo escribiste y no leíste de vuelta.

## Flujo

### 1. Preflight

- Salud de Logic: usa `logic_system` (health) y confirma que los canales necesarios están `ready`. Si algo falla, pide al usuario correr `LogicProMCP doctor` y para.
- Revisa `logic://project/audit` y `logic://project/info`. Hay un proyecto abierto y es el correcto.
- **Respaldo:** `logic_project` → `save_as` a una copia (ej. `<nombre>_pre-engineer.logicx`) antes de mutar nada. Verifica que el paquete existe.
- Nota de locale: si el Logic del usuario está en español (es-ES), la mayoría de las etiquetas de UI están *derivadas*, no medidas. Si fallan operaciones de UI de forma rara, sugiere probar con Logic en inglés.

### 2. Obtener la receta

- Mezcla: pide al sound engineer `judge_mix` con los stems (rutas de archivos exportados).
- Mastering: `judge_master` con el archivo estéreo y la plataforma (`spotify`, `apple_music`, `youtube`, `amazon`, `tidal`, `cd`, `podcast`).
- Si no hay audio exportado aún, pídelo o haz primero un bounce (ver paso 5).
- Si la receta es `send_back`: para (regla 1).

### 3. Mapear y aplicar, en el orden de la receta

| Acción de la receta | Dónde va en Logic | Estado hoy |
|---|---|---|
| `fader` (`target`: vocal, drums, bass...) | `logic_mixer`, volumen de la pista correspondiente | ✅ Verificable: lee el valor actual de `logic://mixer`, aplica el delta en dB, lee de vuelta |
| `pan` | `logic_mixer`, pan | ✅ Verificable (la receta actual no lo emite) |
| `eq.high_pass`, `eq.bell`, `eq.shelf` | Insertar el EQ stock con `logic_plugins.insert_verified` en un slot libre (`get_inventory` primero) | ⚠️ Inserción verificable; **freq/gain/Q no**: `set_param_verified` solo verifica `threshold` del Compressor |
| `compressor` | Insertar Compressor stock con `insert_verified` | ⚠️ `threshold` verificable; ratio/attack/release no |
| `limiter` | Insertar Limiter stock con `insert_verified` | ⚠️ Inserción sí; `ceiling` no verificable |
| `de_esser`, `dynamic_eq`, `stereo.width` | Plugin stock equivalente si existe | ❌ Sin mapeo verificado: propón al usuario hacerlo a mano |
| `dither` | No hay mapeo verificado | ❌ Dile al usuario que lo aplique manualmente al hacer el bounce final |

Dónde insertar según `target`:

- `master` → pista Stereo Out.
- `mix_bus` / `bus` → el bus correspondiente.
- `vocal`, `drums`, `bass`, `stem` → la pista cuyo nombre coincida. **Si hay ambigüedad, pregunta al usuario** (los targets de mutación deben ser explícitos).

Para parámetros que `set_param_verified` no cubre:

1. Revisa en el catálogo de operaciones si existe otra vía de escritura (mcp_logic menciona el canal Scripter para escritura de parámetros).
2. Si la usas, etiqueta el resultado como **aplicado sin verificar** y apóyate en el paso 5 para comprobarlo midiendo.
3. Si no hay vía, déjalo como **pendiente manual** con los valores exactos de la receta (freq, gain, Q, ratio, attack, ceiling).

Aplica una acción a la vez y anota el resultado de cada una.

### 4. Reporte intermedio

Muestra una tabla en el idioma del usuario:

| # | Acción | Valores | Estado |
|---|---|---|---|
| 1 | eq.high_pass master | 25 Hz, 12 dB/oct | aplicado sin verificar |

Estados posibles: `aplicado y verificado`, `aplicado sin verificar`, `pendiente manual`, `fallo`, `omitido`.

### 5. Cerrar el ciclo (medir de nuevo)

1. `logic_project.bounce` de la mezcla o del máster. mcp_logic rechaza el bounce si hay pistas External MIDI / GM Device con regiones MIDI; si pasa, pide mover ese material a Software Instrument.
2. Verifica el archivo con `logic_audio`.
3. Corre `analyze_master` (o `judge_master`) sobre el bounce.
4. Compara contra la meta: true peak bajo el ceiling de la plataforma, LRA razonable, bandas más cerca de la curva de referencia, sin clipping.
5. **Máximo 2 iteraciones** de ajuste. Si después de la segunda no mejora, para y reporta. No entres en bucles.

### 5b. Si empeora

Si la medición posterior es peor (más clipping, más fase negativa, bandas más lejos de la referencia), recomienda volver al respaldo del paso 1 y explica qué acción probablemente lo causó.

## Reporte final

- Qué se aplicó y con qué nivel de verificación.
- Qué queda pendiente manual, con valores exactos.
- Mediciones antes y después (LUFS, true peak, LRA, balance de bandas).
- Ruta del respaldo.
- Advertencias de plataforma (por ejemplo, true peak sobre el ceiling).

## Lo que este skill no hace

- No decide valores de mezcla por su cuenta: la receta viene del sound engineer.
- No sube loudness para "llegar" a un LUFS: las plataformas normalizan y no existe un `target_lufs`.
- No toca el proyecto si el preflight o el respaldo fallan.
