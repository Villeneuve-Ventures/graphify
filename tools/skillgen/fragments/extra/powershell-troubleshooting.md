## Troubleshooting

### PowerShell 5.1: Vertical scrolling stops working

If vertical scrolling breaks in PowerShell after running graphify, this can be caused by ANSI escape sequences from the `graspologic-native` library. Graphify suppresses this output, but if you still see the issue:

1. **Upgrade graphify**: `& (Get-Content graphify-out\.graphify_python) -E -P -B -m pip install --upgrade graphifyy`
2. **Use Windows Terminal** instead of the legacy PowerShell console — Windows Terminal handles ANSI codes correctly
3. **Reset your terminal**: close and reopen PowerShell
4. **Skip graspologic-native**: uninstall it (`& (Get-Content graphify-out\.graphify_python) -E -P -B -m pip uninstall graspologic-native`) and graphify will fall back to NetworkX's built-in Louvain algorithm, which produces no ANSI output

---
