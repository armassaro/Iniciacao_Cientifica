```dataview
task from "5. Anotações de leitura" where !completed 
```
- [x] Testar ganho com resistor de 1M
	_[20/05/2026 10:25]_ O ganho com resistor de 1M não vai ser utilizado mais, por conta do interesse de utilizar um resistor de valor menor (22K) para produzir um teto de 3,3V para ser acoplado a um ADC externo e depois colocado num ESP32. 
- [ ] Entregar esquema do KiCad pro Gabriel imprimir
- [x] Pesquisar informações sobre o ADC do ESP32, como taxa máxima de leituras por segundo
	_[20/05/2026 10:26]_ A taxa máxima de leituras do ESP32 para dispositivos analógicos é próxima de 100kHz, mas para fins práticos irei testar com apenas 10kHz
- [ ] Pesquisar sobre o funcionamento dos sistemas Epsilon e Luxapose
- [x] Começar a programar o algoritmo de leitura que rodará no ESP32, assim como a comunicação entre o ESP32 e o computador para recebimento de informações
	_[20/05/2026 10:30]_ Já estou com o código do ESP32 se utilizando de freeRTOS para fazer leituras consistentes e envio das informações para um computador via rede local