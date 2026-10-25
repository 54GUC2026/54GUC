# Profile workshop

A profile is the bridge between a router's native replies and the information 54GUC can display and use. Improving one can be a satisfying small project: understand a reply, map a useful field, and see it appear in the app.

You can start with a router that is only partly supported, a new firmware variant, or an idea for a device we haven't covered yet. An early [profile proposal](https://github.com/54GUC2026/54GUC/issues/new?template=profile-proposal.yml) is a good place to compare notes.

## 1. Find a starting point

Browse [`modem_profiles/`](../modem_profiles) for a related model or protocol family. The existing families cover Huawei HiLink, MikroTik RouterOS, TP-Link DEV2, Xiaomi MiWiFi, ZTE GoForm and ZTE ZWRT/UBUS.

A related profile is a useful development template. Fork the repository, copy a profile in your working branch, give the draft its own identity, and describe the model and firmware you are exploring. Keeping the original gives you a useful comparison while you experiment.

If the device uses a different protocol, share what you have found. It may need an adapter change as well as a profile, and that can be explored as a separate piece of work.

## 2. Pick something useful and small

Good first experiments include a signal reading, traffic counter, temperature, firmware string or an alternative name for an existing field. Look at the profile's `requests`, `preprocessing` and `fields` sections, then find a similar mapping in another profile.

For your chosen field, collect a small example and ask:

- Where does this value come from?
- What quantity and unit does it represent?
- Is the reply a number, a string, a list or a nested object?
- What happens when the field is absent, zero, or formatted differently?

Public API documentation and comparisons with the router's own page can help. [Protocol references](PROTOCOL_REFERENCES.md) offer some starting points. It's fine to mark an interpretation as a question while you're investigating it.

## 3. Try the mapping offline

Turn the example into a small test, using synthetic data or a shareable edited example. The [test guide](TESTING.md) explains the environment and how to run a focused test. Existing parser and profile tests show how other native formats are handled.

Try both the familiar format and the new one. Useful extra examples include a missing field, a valid zero, an unexpected unit or two conflicting values. That helps a flexible reader accept more understandable replies while keeping its meaning clear.

Profiles use the application's fixed codecs. When an existing expression isn't enough, a small change to the relevant family parser or codec may be the next step. Include it in the proposal rather than trying to fit everything into a profile expression.

## 4. Explore it on your router

A separate development build lets you try your draft on your hardware. **Observe only** is a convenient first step: compare a few readings with the router's page and note what works and what is still unclear.

Monitoring and control support can develop separately. A profile that reads more information is already useful. For a control experiment, recording the request, the router's response and a fresh reading afterward helps explain what the device actually did.

## 5. Share the next step

Open a draft pull request or update your proposal with the profile, the example and what you learned. Include the model/firmware and tests you tried. Feedback from another owner is particularly helpful; a different response is often the clue that improves the next revision.

No hardware to hand? You can still help with format research, synthetic examples, tests, documentation or reviewing someone else's draft. You're also welcome to explore a different approach in your own branch.

<sub>See [project notes](PROJECT_NOTES.md) for practical sharing and hardware-experiment tips.</sub>
