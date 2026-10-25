# Router protocol research

Curious about how a router reports its information? These references are starting points from the project's format research. They can help you compare a native reply, understand a field or explore another firmware. New references and observations are welcome in a [profile proposal](../CONTRIBUTING.md#explore-a-profile).

- https://www.itu.int/dms_pub/itu-t/opb/sp/T-SP-E.212A-2017-PDF-E.pdf
  Original code reference: `arch4/foundation/network_country_reference.py:1`.
- https://gist.github.com/strazzere/8901671795dfeb102ad3
  Original code reference: `arch4/modem/huawei_radio_mode.py:139`.
- https://confusedbird.com/thread-214.html
  Original code reference: `arch4/modem/huawei_parsers.py:236`.
- https://cdn.awsde0-fusion.fds.api.mi-img.com/xiaoqiang/rom/cb0401v2/miwifi_cb0401v2_R03A05-FOTA_firmware_d61ca_3.0.85_INT_ispver-3.0.8.bin
  Original code reference: `arch4/modem/xiaomi_miwifi_tls.py:1`.
- https://github.com/Salamek/huawei-lte-api/blob/f416c63d2a01d7e1d743a2d039ae2d1ff6b71df5/huawei_lte_api/api/Net.py
  Original code reference: `arch4/modem/huawei_radio_mode.py:3`.
- https://github.com/gazzasat/huawei-cpe/blob/33e7f1a68afbc959da7e9bea7d4e2ab7027db45f/src/huawei-bands-util.js
  Original code reference: `arch4/modem/huawei_radio_mode.py:3`.
- https://github.com/yuan-666/cpemanager/blob/09f3c5c00d951639b640eec8310e6f0f52738e77/src/cpemanager/client.py
  Original code reference: `arch4/modem/huawei_radio_mode.py:3`.
- https://github.com/paulo-correia/ZTE_API_and_Hack
  Original code reference: `arch4/tests/test_zte_goform_wifi.py:9`.


The XML example describes a model-level format. The manufacturer firmware link was used for the server-certificate research. Other public clients provided useful examples of protocol grammar; they are not runtime dependencies of these adapters. The MCC table supports network-country matching.

For a practical first experiment, see the [profile workshop](ROUTER_PROFILES.md).

<sub>Sharing tips and the licence link are in [project notes](PROJECT_NOTES.md); existing third-party notices remain with their source material.</sub>
