local Module = {}

function Module.greet(name)
	return "Hello, " .. name
end

function Module.ready(value)
	return value == true
end

return Module
