# Lo que un lector de pantalla (Narrador, NVDA) puede leer de la ventana de Athena:
# recorre el arbol de UI Automation de la ventana y cuenta los controles con nombre.
# Uso: powershell -File uia_lector.ps1 "<titulo de la ventana>" <fichero_salida.json>
param([string]$Titulo, [string]$Salida)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$raiz = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, $Titulo)
$ventana = $null
for ($i = 0; $i -lt 30 -and $ventana -eq $null; $i++) {
    $ventana = $raiz.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
    if ($ventana -eq $null) { Start-Sleep -Milliseconds 500 }
}
if ($ventana -eq $null) { '{"error":"ventana no encontrada"}' | Out-File -Encoding utf8 $Salida; exit 2 }
$todos = $ventana.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition)
$filas = @()
foreach ($e in $todos) {
    $filas += [pscustomobject]@{
        tipo = $e.Current.ControlType.ProgrammaticName
        nombre = $e.Current.Name
        enfocable = $e.Current.IsKeyboardFocusable
    }
}
$conNombre = @($filas | Where-Object { $_.nombre -ne '' })
$resumen = [pscustomobject]@{
    ventana = $Titulo
    elementos = $filas.Count
    con_nombre = $conNombre.Count
    tipos = ($filas | Group-Object tipo | ForEach-Object { "$($_.Name)=$($_.Count)" })
    nombres = ($conNombre | Select-Object -First 60 | ForEach-Object { "$($_.tipo):$($_.nombre)" })
}
$resumen | ConvertTo-Json -Depth 4 | Out-File -Encoding utf8 $Salida
