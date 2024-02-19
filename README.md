# Philip

Also known as Phillip. Depends where you look.

## What is it?

Philip is a [Matrix](https://matrix.org/) bot, written in Python, using [Nio-Bot](https://nio-bot.dev).

This is more of a tech demo than anything functional or purpose-built, however here you can see
some simple design concepts, as well as some advanced features of nio-bot.

This bot is publicly running on `@philip:nexy7574.co.uk` if you want to try it out - just invite it.
The prefix is `!`.

You can also run your own. See the [example config file](./config.example.toml). Personally, I run this via
a systemd service, but it should be easy enough to put in a docker container or otherwise.

**Notice**: There is no guarantee of the functionality of this bot, as it uses bleeding edge features from NioBot.
You should make sure you do not trust it with anything sensitive, and you should not rely on it for anything important.
It is recommended you isolate the bot from the rest of your system, be that through containers, a separate user account,
or otherwise. I do not think my code is that bad, but I do not audit this bot - there may be gaping security holes,
and there are very easy to find attack vectors (such as the `eval` command).
