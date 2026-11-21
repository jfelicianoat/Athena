# Lo que un lector de pantalla puede leer de una consola: busca la ventana por titulo y
# extrae su texto con el TextPattern de UI Automation (lo mismo que usan Narrador y NVDA).
# Uso: powershell -File uia_consola.ps1 "<parte del titulo>" <fichero_salida.json>
param([string]$Titulo, [string]$Salida)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$raiz = [System.Windows.Automation.AutomationElement]::RootElement
$ventana = $null
for ($i = 0; $i -lt 40 -and $ventana -eq $null; $i++) {
    foreach ($v in $raiz.FindAll([System.Windows.Automation.TreeScope]::Children,
            [System.Windows.Automation.Condition]::TrueCondition)) {
        if ($v.Current.Name -like "*$Titulo*") { $ventana = $v; break }
    }
    if ($ventana -eq $null) { Start-Sleep -Milliseconds 500 }
}
if ($ventana -eq $null) { '{"error":"consola no encontrada"}' | Out-File -Encoding utf8 $Salida; exit 2 }
# Tiempo para que el programa escriba su primera pantalla (arranque de Python incluido).
Start-Sleep -Seconds 8
$texto = ""
$todos = $ventana.FindAll([System.Windows.Automation.TreeScope]::Subtree,
    [System.Windows.Automation.Condition]::TrueCondition)
foreach ($e in $todos) {
    $patron = $null
    if ($e.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$patron)) {
        $texto += $patron.DocumentRange.GetText(-1)
    }
}
[pscustomobject]@{
    ventana = $ventana.Current.Name
    tiene_texto_legible = ($texto.Trim().Length -gt 0)
    texto = $texto.Trim()
} | ConvertTo-Json | Out-File -Encoding utf8 $Salida
