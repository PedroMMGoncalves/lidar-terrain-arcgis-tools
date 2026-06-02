# Especificação Técnica — ArcGIS Pro Python Toolbox

## Mining Terrain Factor Toolbox

Documento de instruções para implementação. Destinatário: Claude Code.

---

## 0. Regras de projeto (não negociáveis)

- Sem em dashes em qualquer comentário, docstring ou string. Usar vírgulas, parênteses ou reescrever.
- Sem linhas `Co-Authored-By` em nenhum commit ou ficheiro.
- Ambiente Python: o ambiente clonado do ArcGIS Pro (arcpy). Se for criado ambiente auxiliar, usar conda, nunca venv. Documentar dependências.
- EPSG sempre explícito no código e nos logs. Nunca assumir CRS silenciosamente.
- Fail loud. Nenhum erro silencioso. Se uma pré-condição falha (CRS errado, intervalos com buracos, campo em falta), a ferramenta pára com mensagem clara via `arcpy.AddError` e levanta excecao.
- Sem commits automáticos. O código nao executa git.
- Desenvolvedor solo. Preferir simplicidade e legibilidade a abstracoes. Sem sobre engenharia. Codigo comentado de forma util, nao redundante.
- Idempotencia. Reexecucao nao deve rebentar. Controlo de overwrite explicito.

---

## 1. Objetivo e ambito

Construir uma unica ArcGIS Pro Python Toolbox (`.pyt`) que processa, em batch, dados de elevacao LiDAR da DGT para gerar fatores topograficos destinados a uma analise multicriterio (MCDA) de aptidao para energias renovaveis (foco solar fotovoltaico) em antigas zonas mineiras de Portugal Continental.

A toolbox e o motor de derivacao de fatores. A combinacao ponderada (weighted overlay) e a aplicacao de exclusoes (REN, RAN, PDM) sao passos a jusante feitos por terceiros e NAO fazem parte desta toolbox.

Contexto de dados:
- Tiles LiDAR DGT: GeoTIFF (`.tif`), grelha regular, contiguos (tocam se nas arestas, sem sobreposicao real), elevacao em metros.
- CRS do projeto: ETRS89 / PT-TM06, EPSG:3763. Unidades horizontais e verticais em metros, logo Z factor = 1.
- Dois produtos de elevacao por area: MDT (DEM, terreno) e MDS (DSM, superficie). Operacao independente para cada.
- Areas de interesse: shapefile (ou feature class) de poligonos de minas, dezenas a centenas de feicoes. Cada feicao tem um atributo `Mina` que da nome ao output. Os valores contem caracteres especiais (acentos, c cedilha) que tem de ser saneados.

Escala de processamento esperada: ~200 poligonos, ~800 tiles. Um poligono pode intersetar varios tiles. Poligonos podem partilhar tiles (redundancia aceite, cada poligono e independente).

---

## 2. Arquitetura

Uma `.pyt` com quatro toolsets. Modulo utilitario partilhado para garantir single source of truth no naming e nas funcoes comuns.

```
MiningTerrainToolbox.pyt        # define a Toolbox e regista as Tools
helpers.py                      # modulo partilhado (sanitizacao, CRS, naming, validacao)
reclass_rules_example.csv       # exemplo opcional, ver nota na Tool 3
README.md                       # instrucoes de uso e dependencias
```

> Nota de implementacao: por decisao posterior, os helpers e os testes vivem dentro do
> proprio `MiningTerrainToolbox.pyt` (ficheiro unico), nao em `helpers.py` nem em
> `tests/test_helpers.py`. O desenho funcional desta seccao mantem se; muda so o
> empacotamento. Ver CLAUDE.md, seccao Target structure.

Toolsets (categorias no `.pyt`):

1. **Mosaicking** — Tool: `BuildMosaicsByPolygon`
2. **Surfaces** — Tool: `DeriveSurfaces`
3. **Reclassification** — Tool: `ReclassifyFactor`

(Irradiacao solar via `Area Solar Radiation` fica FORA do ambito atual. Nao implementar agora. Deixar comentario no codigo a assinalar onde uma quinta tool entraria no futuro, sem stub funcional.)

Todas as tools:
- Recebem I/O explicito do utilizador (pasta de entrada, pasta de saida). Nada e inferido por convencao oculta.
- Processam em batch.
- Verificam a licenca Spatial Analyst no inicio (`arcpy.CheckExtension("Spatial")`) e fazem checkout. Fail loud se indisponivel.

---

## 3. Modulo partilhado `helpers.py`

Funcoes obrigatorias:

### 3.1 `sanitize_name(raw_name: str) -> str`

Converte o valor do atributo `Mina` num nome seguro para ArcGIS e sistema de ficheiros.

Regras:
1. Normalizar unicode com `unicodedata.normalize("NFD", s)` e remover marcas diacriticas (`unicodedata.combining`). Isto trata acentos. Tratar `c cedilha` para `c`, etc.
2. Substituir espacos e separadores por underscore.
3. Remover tudo o que nao seja `[A-Za-z0-9_]`.
4. Colapsar underscores multiplos num so.
5. Se o resultado comecar por digito, prefixar com `M_` (ArcGIS nao aceita nomes de raster a comecar por digito em alguns contextos).
6. Truncar a um limite seguro (sugestao: 40 caracteres) para deixar margem para sufixos como `_DEM_ASPECT_RCL`.
7. Se duas minas saneadas colidirem no mesmo nome, anexar sufixo numerico incremental (`_2`, `_3`). A funcao em si trata um nome; a deteccao de colisao e feita pelo chamador, que mantem um set de nomes ja usados. Documentar isto.

A funcao deve registar via `arcpy.AddMessage` quando altera um nome, para rastreabilidade.

### 3.2 `assert_projected_crs(dataset_path, expected_epsg=3763)`

Le o spatial reference do dataset. Fail loud se:
- O CRS for geografico (graus). Mensagem explicita: analise de declives em graus produz resultados invalidos.
- O EPSG diferir do esperado, com aviso (warning, nao erro fatal, mas registado de forma proeminente) caso o utilizador esteja a usar outro CRS projetado conscientemente. Decisao: para Slope/Aspect, CRS geografico e ERRO FATAL; EPSG projetado diferente do esperado e WARNING.

### 3.3 `check_crs_match(fc_a, fc_b) -> bool`

Compara CRS de duas fontes (poligonos vs tiles). Usado na Tool 1. Fail loud se divergirem. Politica acordada: NAO reprojetar silenciosamente. Parar com erro e instruir o utilizador a reprojetar previamente. (Confirmar com o utilizador se prefere reprojecao automatica com log; default desta spec e parar.)

### 3.4 `build_output_name(mina: str, source: str, product: str = None, reclass: bool = False) -> str`

Centraliza a convencao de nomes. Garante que as tres tools produzem e parseiam nomes coerentes.

Convencao:
- Mosaico: `{Mina}_{SOURCE}` onde SOURCE in {`DEM`, `DSM`} -> ex. `MinaA_DEM`
- Superficie: `{Mina}_{SOURCE}_{PRODUCT}` -> ex. `MinaA_DEM_SLOPE`
  - PRODUCT in {`SLOPE`, `ASPECT`, `HILLSHADE`, `PROFC`, `PLANC`}
- Reclassificado: sufixo `_RCL` -> ex. `MinaA_DEM_SLOPE_RCL`

Extensao `.tif` adicionada na escrita, nao no nome logico.

### 3.5 `parse_source_and_product(filename: str) -> dict`

Inverso de `build_output_name`. Permite as Tools 2 e 3 saberem o que receberam (qual a mina, qual a fonte DEM/DSM, qual o produto) a partir do nome do ficheiro. Robusto a `.tif`. Devolve dict com keys `mina`, `source`, `product` (None se for mosaico base).

---

## 4. Tool 1 — `BuildMosaicsByPolygon` (toolset Mosaicking)

### Funcao
Para cada poligono de mina: aplica buffer, seleciona os tiles que intersetam o poligono bufferizado, faz mosaico desses tiles, escreve com o nome saneado da mina. Operacao executada separadamente para a pasta de tiles DEM e para a pasta de tiles DSM (o utilizador corre duas vezes ou a tool aceita ambas as pastas; ver parametros).

### Parametros

| # | Nome | Tipo | Direcao | Notas |
|---|------|------|---------|-------|
| 0 | `in_polygons` | Feature Layer | Input | Shapefile/FC de minas |
| 1 | `mina_field` | Field (obtido de 0) | Input | Campo que da o nome. Default sugerido `Mina` |
| 2 | `dem_tiles_folder` | Folder | Input | Pasta com tiles DEM `.tif`. Opcional |
| 3 | `dsm_tiles_folder` | Folder | Input | Pasta com tiles DSM `.tif`. Opcional |
| 4 | `buffer_distance` | Linear Unit | Input | Valor unico aplicado a todas as minas. Ex. 5 Kilometers |
| 5 | `out_folder` | Folder | Output | Pasta raiz de saida |
| 6 | `output_structure` | String (choice) | Input | `per_mina_subfolder` ou `flat`. Default `per_mina_subfolder` |
| 7 | `pixel_type` | String (choice) | Input | Default `32_BIT_FLOAT`. Expor mas avisar que LiDAR DGT e float |
| 8 | `mosaic_method` | String (choice) | Input | Para overlaps. Default `FIRST` (tiles contiguos sem overlap real). Opcoes: FIRST, MEAN, LAST, BLEND |

Pelo menos uma das pastas (2 ou 3) tem de ser fornecida. Validar em `updateMessages`.

### Logica

1. Checkout Spatial Analyst (necessario a jusante; aqui o mosaico em si nao precisa, mas manter coerencia. O mosaico usa `Mosaic To New Raster` do core, nao SA. Nao fazer checkout desnecessario nesta tool especifica salvo se util. Decisao: nesta tool NAO e preciso SA. Nao fazer checkout aqui.)
2. Validar CRS dos poligonos e dos tiles via `check_crs_match`. Construir um indice de tiles uma vez por pasta:
   - Para cada tile, obter o extent (como os tiles sao grelha regular, o extent retangular E a footprint exata). Guardar (caminho, extent/geometria).
   - Implementacao recomendada: criar uma feature class em memoria (`in_memory`) com um poligono por tile e um campo de texto com o caminho absoluto. Isto permite Spatial Join eficiente. Alternativa mais leve: testar interseccao extent a extent em Python. Para ~800 tiles, qualquer das vias serve. Preferir a via `in_memory` + Spatial Join por robustez e clareza.
3. Para cada poligono:
   a. Aplicar buffer (`arcpy.analysis.Buffer` ou geometria em memoria) com `buffer_distance`.
   b. Selecionar tiles cujo footprint interseta o poligono bufferizado.
   c. Se zero tiles intersetam: registar WARNING com o nome da mina e continuar (nao abortar o batch inteiro por uma mina sem cobertura).
   d. Sanear nome da mina, gerir colisoes (set de nomes usados).
   e. `Mosaic To New Raster` dos tiles selecionados para o output. Numero de bandas 1. Pixel type conforme parametro.
   f. Escrever em `out_folder` (subpasta da mina se `per_mina_subfolder`).
4. Idempotencia: se o output ja existir, comportamento controlado por `arcpy.env.overwriteOutput`. Expor isto e registar skip/overwrite.
5. Progresso: `arcpy.SetProgressor` com contagem de minas. Mensagem por mina processada (n de tiles, nome final).
6. Relatorio final: total de minas processadas, minas sem cobertura, colisoes de nome resolvidas.

### Output
`{Mina}_DEM.tif` e/ou `{Mina}_DSM.tif` por cada poligono, na estrutura escolhida.

---

## 5. Tool 2 — `DeriveSurfaces` (toolset Surfaces)

### Funcao
Corre sobre uma pasta de mosaicos (output da Tool 1) e gera as superficies topograficas selecionadas, em batch, preservando a referencia da mina e da fonte no nome.

### Superficies suportadas (lista fechada)
- Slope (graus)
- Aspect
- Hillshade (tradicional ou multidirecional)
- Curvature: profile e plan (dois ficheiros)

### Parametros

| # | Nome | Tipo | Direcao | Notas |
|---|------|------|---------|-------|
| 0 | `in_mosaics_folder` | Folder | Input | Pasta com os `.tif` de mosaico (Tool 1) |
| 1 | `recurse_subfolders` | Boolean | Input | Default True (apanha estrutura per_mina_subfolder) |
| 2 | `out_folder` | Folder | Output | Pasta raiz de saida |
| 3 | `output_structure` | String (choice) | Input | `per_mina_subfolder` ou `flat`. Default igual ao input |
| 4 | `do_slope` | Boolean | Input | Default True |
| 5 | `do_aspect` | Boolean | Input | Default True |
| 6 | `do_hillshade` | Boolean | Input | Default True |
| 7 | `do_curvature` | Boolean | Input | Default True (gera profile e plan) |
| 8 | `z_factor` | Double | Input | Default 1. Aviso se CRS geografico |
| 9 | `hillshade_type` | String (choice) | Input | `Multidirectional` (default) ou `Traditional` |
| 10 | `hillshade_azimuth` | Double | Input | Default 315. So relevante se Traditional |
| 11 | `hillshade_altitude` | Double | Input | Default 45. So relevante se Traditional |

`updateParameters`: quando `hillshade_type` = Multidirectional, desativar (greyed out) os parametros 10 e 11.

### Logica

1. Checkout Spatial Analyst. Fail loud se indisponivel.
2. Descobrir todos os `.tif` na pasta (recursivo se aplicavel). Para cada, usar `parse_source_and_product` para obter `mina` e `source`. Ignorar (com mensagem) ficheiros que nao sigam a convencao de mosaico base (ou seja, processar apenas os que sao DEM/DSM puros, nao reprocessar superficies ja geradas se estiverem na mesma pasta).
3. Para cada mosaico de entrada:
   - Validar CRS via `assert_projected_crs` (geografico = erro fatal para Slope/Aspect).
   - Slope: `arcpy.sa.Slope(in_raster, "DEGREE", z_factor)`. Guardar `{Mina}_{SOURCE}_SLOPE.tif`.
   - Aspect: `arcpy.sa.Aspect(in_raster)`. Guardar `..._ASPECT.tif`. (Aspect gera -1 para flat; nao tratar aqui, e tratado na reclassificacao.)
   - Hillshade:
     - Multidirectional: usar a ferramenta apropriada. Em arcpy moderno e `arcpy.sa.Hillshade` nao cobre multidirecional; usar `arcpy.ddd.MultidirectionalHillshade` ou o raster function equivalente. VERIFICAR a API disponivel na versao instalada e usar a correta. Fail loud com mensagem util se a funcao nao existir na versao do utilizador.
     - Traditional: `arcpy.sa.Hillshade(in_raster, azimuth, altitude, "SHADOWS", z_factor)`.
     - Output: `..._HILLSHADE.tif` em ambos os casos.
   - Curvature: usar `arcpy.sa.SurfaceParameters` (moderno) ou `arcpy.sa.Curvature` (classico). Decisao: preferir o classico `Curvature` que devolve diretamente os rasters de profile e plan como outputs opcionais, mais simples para obter ambos numa chamada. `arcpy.sa.Curvature(in_raster, z_factor, out_profile_curve, out_plan_curve)`. Guardar `..._PROFC.tif` e `..._PLANC.tif`.
4. Idempotencia, progresso e relatorio final como na Tool 1.

### Notas tecnicas
- O Aspect do ArcGIS produz valores 0 a 360 com -1 = Flat. Documentar.
- NAO assumir que `SurfaceParameters` esta disponivel em todas as versoes. Verificar. Usar a via mais estavel.

### Output
Por cada mosaico: `{Mina}_{SOURCE}_SLOPE.tif`, `_ASPECT.tif`, `_HILLSHADE.tif`, `_PROFC.tif`, `_PLANC.tif` conforme seleccao.

---

## 6. Tool 3 — `ReclassifyFactor` (toolset Reclassification)

### Funcao
Reclassifica rasters de fator (Slope e Aspect) em classes ordinais definidas pelo utilizador, em batch. As classes sao definidas via Value Table no dialog da ferramenta (opcao B acordada). Iteravel para CSV no futuro.

Aplica se a Slope e a Aspect. Curvature e Hillshade nao sao reclassificados nesta versao.

### Modelo de classes

Value Table com tres colunas: `class_id` (Long), `min_value` (Double), `max_value` (Double).

Regras de interpretacao acordadas:
- Intervalo semiaberto `[min, max)`: inclui o minimo, exclui o maximo. EXCETO a ultima classe (maior max), que inclui ambos os extremos `[min, max]`. Determinar "ultima" pelo maior `max_value` presente.
- O mesmo `class_id` pode aparecer em multiplas linhas. Multiplos intervalos mapeiam para o mesmo valor de saida. Isto suporta o Aspect circular: o utilizador parte a classe Norte em duas linhas (ex. 315 a 360 e 0 a 45, ambas com `class_id` 1). A ferramenta NAO deteta wraparound; trata cada linha como intervalo linear simples. O utilizador faz a particao.
- Intervalos NAO podem ter buracos nem sobreposicoes (exceto a sobreposicao implicita de mesmo `class_id`, que e intencional). VALIDACAO FAIL LOUD: antes de reclassificar, verificar a cobertura. Se existir um gap (valor que nao cai em nenhuma classe) dentro do dominio min global a max global, ou uma sobreposicao entre `class_id` DIFERENTES, parar com `arcpy.AddError` indicando os limites problematicos. Mesmo `class_id` sobreposto e permitido.

### Flat (apenas Aspect)
Parametro separado `flat_class_value` (Long, opcional). Quando fornecido e o fator for Aspect, o valor -1 (Flat) e mapeado para esse `class_id`. Quando o fator for Slope, o parametro e ignorado (Slope nao tem -1). A ferramenta deteta o fator pelo nome do ficheiro (`parse_source_and_product` -> product ASPECT vs SLOPE).

### Parametros

| # | Nome | Tipo | Direcao | Notas |
|---|------|------|---------|-------|
| 0 | `in_folder` | Folder | Input | Pasta com os rasters de fator (Tool 2) |
| 1 | `recurse_subfolders` | Boolean | Input | Default True |
| 2 | `factor_to_process` | String (choice) | Input | `SLOPE`, `ASPECT`, ou `BOTH`. Filtra que ficheiros processar |
| 3 | `slope_classes` | Value Table | Input | Colunas class_id, min, max. So usado se factor inclui SLOPE |
| 4 | `aspect_classes` | Value Table | Input | Colunas class_id, min, max. So usado se factor inclui ASPECT |
| 5 | `flat_class_value` | Long | Input | Opcional. Classe do Flat (-1) no Aspect |
| 6 | `out_folder` | Folder | Output | Pasta raiz de saida |
| 7 | `output_structure` | String (choice) | Input | `per_mina_subfolder` ou `flat` |
| 8 | `nodata_for_unmapped` | Boolean | Input | Default True. Valores fora de todas as classes vao para NoData (so relevante se validacao permitir, normalmente nao havera por causa do fail loud) |

`updateParameters`: ativar `slope_classes` so quando factor inclui SLOPE; `aspect_classes` e `flat_class_value` so quando inclui ASPECT.

### Logica

1. Checkout Spatial Analyst.
2. Validar as Value Tables relevantes (cobertura, gaps, sobreposicoes entre class_id diferentes). Fail loud antes de processar qualquer raster.
3. Construir a remap. Implementacao recomendada: `arcpy.sa.RemapRange` a partir das linhas do Value Table. Para o Flat no Aspect, adicionar uma entrada de remap especifica para o valor -1.
   - Nota tecnica sobre semiaberto vs RemapRange: o `Reclassify` do ArcGIS com `RemapRange` trata os limites como `[min, max]` inclusivo em ambos por defeito, e um valor que esteja exatamente numa fronteira partilhada pode ser atribuido de forma ambigua. Para implementar `[min, max)` de forma deterministica: ordenar as classes por min e construir os RemapRange de modo a que o max de uma classe seja exatamente o min da seguinte; o comportamento do Reclassify atribui o valor de fronteira a uma das classes de forma consistente. SE o comportamento de fronteira do Reclassify nao for suficientemente deterministico para o rigor exigido, implementar a reclassificacao via algebra de mapas com `Con` encadeado ou via `numpy` (RasterToNumPyArray, aplicar logica `[min, max)` explicita, NumPyArrayToRaster), preservando NoData e o CRS. Decidir pela via mais simples que garanta a semantica `[min, max)`. Documentar a decisao tomada e a sua implicacao nas fronteiras.
4. Para cada raster de fator que corresponda ao filtro:
   - `parse_source_and_product` para confirmar SLOPE vs ASPECT e obter mina/source.
   - Aplicar a remap correta (slope_classes para SLOPE, aspect_classes + flat para ASPECT).
   - Guardar `{Mina}_{SOURCE}_{PRODUCT}_RCL.tif`.
   - Preservar NoData. Garantir tipo inteiro no output (classes ordinais).
5. Idempotencia, progresso, relatorio final.

### Output
`{Mina}_{SOURCE}_SLOPE_RCL.tif` e/ou `{Mina}_{SOURCE}_ASPECT_RCL.tif`.

### Nota de ambito
A escala ordinal NAO e normalizada nem garantida comparavel entre fatores nesta versao. O utilizador define intervalos arbitrarios por fator. A harmonizacao para weighted overlay e responsabilidade do passo MCDA a jusante, fora desta toolbox. Documentar isto claramente no README para evitar uso indevido.

---

## 7. Validacao e tratamento de erros (transversal)

- Toda a validacao de pre condicoes corre ANTES de qualquer escrita.
- CRS geografico em input de Slope/Aspect: erro fatal.
- Value Table com gap ou sobreposicao entre class_id diferentes: erro fatal, com indicacao dos limites.
- Mina sem tiles intersetados: warning, continua o batch.
- Nome saneado a colidir: resolver com sufixo, registar.
- Licenca Spatial Analyst indisponivel (Tools 2 e 3): erro fatal no inicio.
- Ficheiro de entrada que nao segue a convencao de nomes: skip com mensagem, nao aborta.
- Usar `arcpy.AddMessage`, `arcpy.AddWarning`, `arcpy.AddError` de forma consistente. Mensagens em portugues ou ingles (escolher um e ser consistente; sugiro ingles para alinhamento com a API arcpy, mas o utilizador decide).

---

## 8. Estrutura de codigo esperada

- `MiningTerrainToolbox.pyt`: classe `Toolbox` com `self.tools = [BuildMosaicsByPolygon, DeriveSurfaces, ReclassifyFactor]`. Cada Tool e uma classe com `category` definindo o toolset, `getParameterInfo`, `updateParameters`, `updateMessages`, `execute`, `isLicensed`.
- `helpers.py`: funcoes da seccao 3. Importado pelo `.pyt`. Garantir que o `.pyt` e o `helpers.py` ficam na mesma pasta e que o import funciona no contexto da toolbox (ArcGIS adiciona a pasta da toolbox ao path; confirmar e, se necessario, manipular `sys.path` no topo do `.pyt`).
- Funcoes de execucao longas devem ser fatoradas em funcoes testaveis fora do contexto arcpy onde possivel (ex. `sanitize_name`, validacao de Value Table). Isto permite testes unitarios simples sem ArcGIS.

---

## 9. Testes minimos a incluir

Sem framework pesado. Um `test_helpers.py` simples (pode correr fora do ArcGIS) que verifica:
- `sanitize_name`: acentos, c cedilha, espacos, inicio com digito, colisao, truncagem.
- Validacao de Value Table: caso valido, caso com gap, caso com sobreposicao entre class_id diferentes, caso com mesmo class_id repetido (deve passar).
- `parse_source_and_product`: round trip com `build_output_name`.

Documentar como correr os testes no README.

---

## 10. README

Deve conter:
- Pre requisitos: ArcGIS Pro versao X, extensao Spatial Analyst, EPSG:3763 nos dados.
- Como adicionar a toolbox ao ArcGIS Pro.
- Fluxo de uso: Tool 1 -> Tool 2 -> Tool 3, com nota de que o I/O e sempre indicado pelo utilizador e a Tool 2 corre sobre output da 1, a Tool 3 sobre output da 2.
- Convencao de nomes completa com exemplos.
- Aviso explicito: a escala ordinal da reclassificacao nao e harmonizada entre fatores; exclusoes REN/RAN/PDM e weighted overlay sao passos externos.
- Como correr os testes.
- Dependencias e ambiente (arcpy, conda se aplicavel).

---

## 11. Pontos deixados em aberto para confirmar com o utilizador durante a implementacao

1. CRS divergente entre poligonos e tiles: esta spec manda PARAR com erro. Confirmar se prefere reprojecao automatica com log.
2. Idioma das mensagens arcpy (ingles vs portugues).
3. Via de implementacao da semantica `[min, max)` na reclassificacao (Reclassify nativo vs numpy). Escolher a mais simples que garanta determinismo nas fronteiras e documentar.
4. API de multidirectional hillshade disponivel na versao instalada do ArcGIS Pro (confirmar `MultidirectionalHillshade` vs raster function).

Nenhum destes deve ser resolvido por adivinhacao silenciosa. Em caso de duvida na implementacao, parar e perguntar.
